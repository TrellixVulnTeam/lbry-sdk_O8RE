import asyncio
import typing
import logging
import binascii

from lbry.dht.node import get_kademlia_peers_from_hosts
from lbry.error import DownloadSDTimeoutError
from lbry.utils import lru_cache_concurrent
from lbry.stream.descriptor import StreamDescriptor
from lbry.blob_exchange.downloader import BlobDownloader
from lbry.torrent.tracker import enqueue_tracker_search

if typing.TYPE_CHECKING:
    from lbry.conf import Config
    from lbry.dht.node import Node
    from lbry.blob.blob_manager import BlobManager
    from lbry.blob.blob_file import AbstractBlob
    from lbry.blob.blob_info import BlobInfo

log = logging.getLogger(__name__)


class StreamDownloader:
    def __init__(self, loop: asyncio.AbstractEventLoop, config: 'Config', blob_manager: 'BlobManager', sd_hash: str,
                 descriptor: typing.Optional[StreamDescriptor] = None):
        self.loop = loop
        self.config = config
        self.blob_manager = blob_manager
        self.sd_hash = sd_hash
        self.search_queue = asyncio.Queue(loop=loop)     # blob hashes to feed into the iterative finder
        self.peer_queue = asyncio.Queue(loop=loop)       # new peers to try
        self.blob_downloader = BlobDownloader(self.loop, self.config, self.blob_manager, self.peer_queue)
        self.descriptor: typing.Optional[StreamDescriptor] = descriptor
        self.node: typing.Optional['Node'] = None
        self.accumulate_task: typing.Optional[asyncio.Task] = None
        self.fixed_peers_handle: typing.Optional[asyncio.Handle] = None
        self.fixed_peers_delay: typing.Optional[float] = None
        self.added_fixed_peers = False
        self.time_to_descriptor: typing.Optional[float] = None
        self.time_to_first_bytes: typing.Optional[float] = None

        async def cached_read_blob(blob_info: 'BlobInfo') -> bytes:
            return await self.read_blob(blob_info, 2)

        if self.blob_manager.decrypted_blob_lru_cache is not None:
            cached_read_blob = lru_cache_concurrent(override_lru_cache=self.blob_manager.decrypted_blob_lru_cache)(
                cached_read_blob
            )

        self.cached_read_blob = cached_read_blob

    async def add_fixed_peers(self):
        def _add_fixed_peers(fixed_peers):
            self.peer_queue.put_nowait(fixed_peers)
            self.added_fixed_peers = True

        if not self.config.fixed_peers:
            return
        if 'dht' in self.config.components_to_skip or not self.node or not \
                len(self.node.protocol.routing_table.get_peers()) > 0:
            self.fixed_peers_delay = 0.0
        else:
            self.fixed_peers_delay = self.config.fixed_peer_delay
        fixed_peers = await get_kademlia_peers_from_hosts(self.config.fixed_peers)
        self.fixed_peers_handle = self.loop.call_later(self.fixed_peers_delay, _add_fixed_peers, fixed_peers)

    async def load_descriptor(self, connection_id: int = 0):
        # download or get the sd blob
        sd_blob = self.blob_manager.get_blob(self.sd_hash)
        if not sd_blob.get_is_verified():
            try:
                now = self.loop.time()
                sd_blob = await asyncio.wait_for(
                    self.blob_downloader.download_blob(self.sd_hash, connection_id),
                    self.config.blob_download_timeout
                )
                log.info("downloaded sd blob %s", self.sd_hash)
                self.time_to_descriptor = self.loop.time() - now
            except asyncio.TimeoutError:
                raise DownloadSDTimeoutError(self.sd_hash)

        # parse the descriptor
        self.descriptor = await StreamDescriptor.from_stream_descriptor_blob(
            self.loop, self.blob_manager.blob_dir, sd_blob
        )
        log.info("loaded stream manifest %s", self.sd_hash)

    async def start(self, node: typing.Optional['Node'] = None, connection_id: int = 0, save_stream=True):
        # set up peer accumulation
        self.node = node or self.node  # fixme: this shouldnt be set here!
        if self.node:
            if self.accumulate_task and not self.accumulate_task.done():
                self.accumulate_task.cancel()
            _, self.accumulate_task = self.node.accumulate_peers(self.search_queue, self.peer_queue)
        await self.add_fixed_peers()
        enqueue_tracker_search(bytes.fromhex(self.sd_hash), self.peer_queue)
        # start searching for peers for the sd hash
        self.search_queue.put_nowait(self.sd_hash)
        log.info("searching for peers for stream %s", self.sd_hash)

        if not self.descriptor:
            await self.load_descriptor(connection_id)

        if not await self.blob_manager.storage.stream_exists(self.sd_hash) and save_stream:
            await self.blob_manager.storage.store_stream(
                self.blob_manager.get_blob(self.sd_hash, length=self.descriptor.length), self.descriptor
            )

    async def download_stream_blob(self, blob_info: 'BlobInfo', connection_id: int = 0) -> 'AbstractBlob':
        if not filter(lambda b: b.blob_hash == blob_info.blob_hash, self.descriptor.blobs[:-1]):
            raise ValueError(f"blob {blob_info.blob_hash} is not part of stream with sd hash {self.sd_hash}")
        blob = await asyncio.wait_for(
            self.blob_downloader.download_blob(blob_info.blob_hash, blob_info.length, connection_id),
            self.config.blob_download_timeout * 10
        )
        return blob

    def decrypt_blob(self, blob_info: 'BlobInfo', blob: 'AbstractBlob') -> bytes:
        return blob.decrypt(
            binascii.unhexlify(self.descriptor.key.encode()), binascii.unhexlify(blob_info.iv.encode())
        )

    async def read_blob(self, blob_info: 'BlobInfo', connection_id: int = 0) -> bytes:
        start = None
        if self.time_to_first_bytes is None:
            start = self.loop.time()
        blob = await self.download_stream_blob(blob_info, connection_id)
        decrypted = self.decrypt_blob(blob_info, blob)
        if start:
            self.time_to_first_bytes = self.loop.time() - start
        return decrypted

    def stop(self):
        if self.accumulate_task:
            self.accumulate_task.cancel()
            self.accumulate_task = None
        if self.fixed_peers_handle:
            self.fixed_peers_handle.cancel()
            self.fixed_peers_handle = None
        self.blob_downloader.close()
