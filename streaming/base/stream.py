# Copyright 2023 MosaicML Streaming authors
# SPDX-License-Identifier: Apache-2.0

"""A dataset, or sub-dataset if mixing, from which we stream/cache samples."""

import json
import os
from tempfile import mkdtemp
from typing import List, Optional, Sequence

import numpy as np
from numpy.typing import NDArray
from typing_extensions import Self

from streaming.base.compression import decompress
from streaming.base.format import Reader, reader_from_json
from streaming.base.format.base.reader import FileInfo
from streaming.base.hashing import get_hash
from streaming.base.index import get_index_basename
from streaming.base.storage import download_file
from streaming.base.util import TICK, wait_for_file_to_exist
from streaming.base.world import World


class Stream:
    """A dataset, or sub-dataset if mixing, from which we stream/cache samples.

    We initialize a StreamingDataset with one or more Streams. Streams may be resampled to achieve
    different mixtures of samples.

    Stream init takes three kinds of arguments:

    * At least one of ``remote`` and ``local`` must exist. If no ``remote``, the data must be
      local. If no ``local``, we cache to a temp directory.

      * ``remote``
      * ``local``

    * At most one of ``proportion``, ``repeat``, or ``samples`` may exist. If none are provided,
      each sample is seen once per epoch. If provided one of these, we derive the others.
      Note that ``proportion`` (relative) and ``repeat``/``samples`` (absolute) are mutually
      incompatible -- you must entirely use one or the other (or neither) for all sub-datasets.

      * ``proportion``
      * ``repeat``
      * ``samples``

    * The remaining arguments are optional knobs for controlling downloading behavior and default
      to ``None``. If ``None``, they take a default value provided to or by the StreamingDataset
      init.

      * ``split``
      * ``download_retry``
      * ``download_timeout``
      * ``validate_hash``
      * ``keep_zip``

    Args:
        remote (str, optional): Remote path or directory to download the dataset from. If ``None``,
            its data must exist locally. Defaults to ``None``.
        local (str, optional): Local working directory to download shards to. This is where shards
            are cached while they are being used. Uses a temp directory if not set. Defaults to
            ``None``.
        split (str, optional): Which dataset split to use, if any. If provided, we stream from/to
            the ``split`` subdirs of  ``remote`` and ``local``. Defaults to ``None``.
        proportion (float, optional): How much to upsample or downsample this sub-dataset, as the
            proportion of the total combined dataset that consists of this sub-dataset. If
            using proportions, all sub-datasets provided together to the StreamingDataset init must
            define their proportions. The total combined number of samples is either the
            StreamingDataset argument "samples_per_epoch" if provided, or kept the same total size
            as the underlying data if not. If provided, must be positive. Defaults to ``None``.
        repeat (float, optional): How much to upsample or downsample this sub-dataset, as a
            multipler on the number of samples. If provided, must be positive. Defaults to
            ``None``.
        samples (int, optional): How much to upsample or downsample this sub-dataset, as the exact
            number of samples. If provided, must be positive. Defaults to ``None``.
        download_retry (int, optional): Number of download re-attempts before giving up. Defaults
            to ``None``.
        download_timeout (float, optional): Number of seconds to wait for a shard to download
            before raising an exception. Defaults to ``None``.
        validate_hash (str, optional): Optional hash or checksum algorithm to use to validate
            shards. Defaults to ``None``.
        keep_zip (bool, optional): Whether to keep or delete the compressed form when decompressing
            downloaded shards. If ``False``, keep if and only if remote is local or no remote.
            Defaults to ``None``.
    """

    def __init__(self,
                 *,
                 remote: Optional[str] = None,
                 local: Optional[str] = None,
                 split: Optional[str] = None,
                 proportion: Optional[float] = None,
                 repeat: Optional[float] = None,
                 samples: Optional[int] = None,
                 download_retry: Optional[int] = None,
                 download_timeout: Optional[float] = None,
                 validate_hash: Optional[str] = None,
                 keep_zip: Optional[bool] = None) -> None:
        self.remote = remote
        self._local = local
        self.local = local or mkdtemp()
        self.split = split or ''

        has_proportion = proportion is not None
        has_repeat = repeat is not None
        has_samples = samples is not None
        if not (0 <= has_proportion + has_repeat + has_samples <= 1):
            raise ValueError('At most one of "proportion", "repeat", and "samples" may be ' +
                             'specified; the others are derived')

        self._proportion = proportion
        if proportion is not None:
            if proportion < 0:
                raise ValueError('Proportion must be non-negative')
            self.proportion = proportion

        self._repeat = repeat
        if repeat is not None:
            if repeat < 0:
                raise ValueError('Repeat must be non-negative')
            self.repeat = repeat

        self._samples = samples
        if samples is not None:
            if samples < 0:
                raise ValueError('Samples must be non-negative')
            self.samples = samples

        self._download_retry = download_retry
        if download_retry is not None:
            if download_retry < 0:
                raise ValueError('Download retry must be non-negative')
            self.download_retry = download_retry

        self._download_timeout = download_timeout
        if download_timeout is not None:
            if download_timeout <= 0:
                raise ValueError('Download timeout must be positive')
            self.download_timeout = download_timeout

        self.validate_hash = validate_hash

        self._keep_zip = keep_zip
        if keep_zip is not None:
            self.keep_zip = keep_zip
            self.safe_keep_zip = self.keep_zip or self.remote in {None, self.local}

    def apply_default(self, default: Self) -> None:
        """Apply defaults, setting any unset fields.

        We use pairs of (name, _name) in order to make type checking happy.

        Args:
            default (Self): Stream containing default values for all optional fields.
        """
        if not (self.remote or self._local):
            raise ValueError('Remote and/or local path must be provided')

        if not self.split:
            self.split = default.split or ''
        if self._download_retry is None:
            self.download_retry = default.download_retry
        if self._download_timeout is None:
            self.download_timeout = default.download_timeout
        if self.validate_hash is None:
            self.validate_hash = default.validate_hash or None
        if self._keep_zip is None:
            self.keep_zip = default.keep_zip
            self.safe_keep_zip = self.keep_zip or self.remote in {None, self.local}

    @classmethod
    def validate_weights(cls, streams: Sequence[Self]) -> bool:
        """Validate stream weights, returning whether relative or absolute weighting was used.

        Args:
            streams (Sequence[Stream]): Every stream comprising the dataset.

        Returns:
            bool: Whether streams are weighted relatively (proportionally).
        """
        # Validate sub-dataset weights ("proportion", "repeat", "samples", or none).
        is_relative = hasattr(streams[0], 'proportion')
        for stream_id, stream in enumerate(streams):
            has_proportion = hasattr(stream, 'proportion')
            has_repeat = hasattr(stream, 'repeat')
            has_samples = hasattr(stream, 'samples')
            if not (0 <= has_proportion + has_repeat + has_samples <= 1):
                raise ValueError(f'Streams must provide at most one of "proportion", "repeat", ' +
                                 f'or "samples" (error in stream {stream_id})')
            if is_relative != has_proportion:
                raise ValueError(f'Relative ("proportion") and absolute ("repeat", "samples", ' +
                                 f'none) sub-dataset weights are incompatible with each other ' +
                                 f'(error in stream {stream_id})')
        return is_relative

    @classmethod
    def apply_weights(cls, streams: Sequence[Self], samples_per_stream: NDArray[np.int64],
                      samples_per_epoch: Optional[int], seed: int) -> int:
        """Given samples per stream, derive each stream's proportion/repeat/samples.

        Modifies streams to save the derived weights.

        Args:
            streams (Sequence[Stream]): The list of streams which comprise the dataset.
            samples_per_stream (NDArray[np.int64]): Underlying samples of each stream.
            samples_per_epoch (int, optional): The resampled size of the epoch after weighting, if
                desired to be different from the underlying size and relative weights are used.
            seed (int): Random number generator seed used to sample evenly.

        Returns:
            int: Derived samples_per_epoch which is always valid.
        """
        # Validate provided weights, determining whether they are relative or absolute.
        are_weights_relative = cls.validate_weights(streams)

        # Derive weights.
        if are_weights_relative:
            # Relative weighting.
            if not samples_per_epoch:
                samples_per_epoch = sum(samples_per_stream)
            proportion_per_stream = np.array([stream.proportion for stream in streams], np.float64)
            proportion_per_stream /= proportion_per_stream.sum()
            pick_per_stream = (samples_per_epoch * proportion_per_stream).astype(np.int64)
            short = samples_per_epoch - pick_per_stream.sum()
            rng = np.random.default_rng(seed)
            indices = rng.choice(len(streams), short, False)
            pick_per_stream[indices] += 1
            repeat_per_stream = pick_per_stream / samples_per_stream
        else:
            # Absolute weighting.
            if samples_per_epoch:
                raise ValueError('Only provide samples_per_epoch when proportionally weighting ' +
                                 'sub-datasets.')
            pick_per_stream = np.zeros(len(streams), np.int64)
            for stream_id, stream in enumerate(streams):
                if hasattr(stream, 'repeat'):
                    samples = int(stream.repeat * samples_per_stream[stream_id])
                elif hasattr(stream, 'samples'):
                    samples = stream.samples
                else:
                    samples = samples_per_stream[stream_id]
                pick_per_stream[stream_id] = samples
            repeat_per_stream = pick_per_stream / samples_per_stream
            proportion_per_stream = pick_per_stream / pick_per_stream.sum()
            samples_per_epoch = sum(pick_per_stream)

        # Now that we know the true props/repeats/picks, inject those back into the streams.
        for stream, proportion, repeat, pick in zip(streams, proportion_per_stream,
                                                    repeat_per_stream, pick_per_stream):
            stream.proportion = proportion
            stream.repeat = repeat
            stream.samples = pick

        return samples_per_epoch

    def _download_file(self, basename: str) -> str:
        """Safely download a file from remote to local cache.

        Args:
            basename (str): Basename of file to download.

        Returns:
            str: Local cache filename.
        """
        # Calculate paths.
        if self.remote is None:
            remote = None
        else:
            remote = os.path.join(self.remote, self.split, basename)
        local = os.path.join(self.local, self.split, basename)

        # Attempt to download, possibly repeating on failure.
        errors = []
        for _ in range(1 + self.download_retry):
            try:
                download_file(remote, local, self.download_timeout)
            except FileNotFoundError:  # Bubble up file not found error.
                raise
            except Exception as e:  # Retry for all other causes of failure.
                errors.append(e)
                continue
            break

        if self.download_retry < len(errors):
            raise RuntimeError(
                f'Failed to download {remote} -> {local}. Got errors:\n{errors}') from errors[-1]

        return local

    def _decompress_shard_part(self, zip_info: FileInfo, zip_filename: str, raw_filename: str,
                               compression: Optional[str]) -> None:
        """Validate and decompress shard data.

        Args:
            zip_info (FileInfo): Compressed file info.
            zip_filename (str): Compressed filename.
            raw_filename (str): Decompressed filename.
            compression (str, optional): Compression algorithm.
        """
        # Load compressed.
        data = open(zip_filename, 'rb').read()

        # Validate what was downloaded.
        if self.validate_hash:
            if get_hash(self.validate_hash, data) != zip_info.hashes[self.validate_hash]:
                raise ValueError(f'Checksum failure: {zip_filename}')

        # Decompress and save that.
        data = decompress(compression, data)  # pyright: ignore
        tmp_filename = raw_filename + '.tmp'
        with open(tmp_filename, 'wb') as out:
            out.write(data)
        os.rename(tmp_filename, raw_filename)

        # Maybe remove compressed to save space.
        if not self.keep_zip and self.remote != self.local:
            os.remove(zip_filename)

    def _download_shard_part(self,
                             raw_info: FileInfo,
                             zip_info: Optional[FileInfo] = None,
                             compression: Optional[str] = None) -> None:
        """Download shard data given metadata for the raw and compressed versions of it.

        MDS format uses joint shards (ie, one file per shard). Other formats supported by streaming
        use split shards (ie, shard data lives in two files per shard: the raw data itself and
        metadata in a separate file).

        Args:
            raw_info (FileInfo): Raw file info.
            zip_info (FileInfo, optional): Zip file info. Defaults to ``None``.
            compression (str, optional): Compression algorithm used for zip_info. Defaults to
                ``None``.
        """
        # If the local raw file already exists, this is a no-op.
        raw_filename = os.path.join(self.local, self.split, raw_info.basename)
        if os.path.isfile(raw_filename):
            return

        # Is compression used?
        if zip_info:
            # Download the compressed form if missing.
            zip_filename = os.path.join(self.local, self.split, zip_info.basename)
            if not os.path.isfile(zip_filename):
                self._download_file(zip_info.basename)

            # Validate and decompress.
            self._decompress_shard_part(zip_info, zip_filename, raw_filename, compression)
        else:
            # Download the raw form.
            self._download_file(raw_info.basename)

            # Validate if requested.
            if self.validate_hash:
                data = open(raw_filename, 'rb').read()
                if get_hash(self.validate_hash, data) != raw_info.hashes[self.validate_hash]:
                    raise ValueError(f'Checksum failure: {raw_filename}')

    def download_shard(self, shard: Reader) -> None:
        """Download the given shard.

        Args:
            shard (Reader): Which shard.
        """
        for raw_info, zip_info in shard.file_pairs:
            self._download_shard_part(raw_info, zip_info, shard.compression)

    def get_shards(self, world: World) -> List[Reader]:
        """Load this Stream's index, retrieving its shard readers.

        Args:
            world (World): Distributed context.

        Returns:
            `List[Reader]: Shard readers.
        """
        # Download the index.
        basename = get_index_basename()
        if world.is_local_leader:
            filename = self._download_file(basename)
        else:
            filename = os.path.join(self.local, self.split, basename)  # pyright: ignore
            wait_for_file_to_exist(filename, TICK, self.download_timeout,
                                   f'{filename} file took too long to download')

        # Load and validate the index.
        obj = json.load(open(filename))
        if obj['version'] != 2:
            raise ValueError(f'Unsupported version: {obj["version"]}')

        # Initialize shard readers according to the loaded info.
        shards = []
        for info in obj['shards']:
            shard = reader_from_json(self.local, self.split, info)
            shards.append(shard)

        return shards

    def init_local_dir(self, shards: List[Reader]) -> List[bool]:
        """Bring a local directory into a consistent state, getting which shards are present.

        Args:
            shards (List[Reader]): List of this stream's shards.

        Returns:
            List[bool]: List of whether each stream shard is present.
        """
        # List the cache directory (so that we hit the filesystem once).
        local_dirname = os.path.join(self.local, self.split)
        ls = set()
        for dirname, _, subfiles in os.walk(local_dirname):
            for subfile in subfiles:
                filename = os.path.join(dirname, subfile)
                ls.add(filename)

        # Determine which shards are present, making local dir consistent.
        are_shards_present = []
        for shard in shards:
            is_shard_present = shard.init_local_dir(ls, self.safe_keep_zip)
            are_shards_present.append(is_shard_present)
        return are_shards_present
