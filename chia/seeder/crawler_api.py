from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, Optional, cast

from chia.protocols import full_node_protocol, wallet_protocol
from chia.protocols.outbound_message import Message
from chia.seeder.crawler import Crawler
from chia.server.api_protocol import ApiMetadata
from chia.server.server import ChiaServer
from chia.server.ws_connection import WSChiaConnection


class CrawlerAPI:
    if TYPE_CHECKING:
        from chia.server.api_protocol import ApiProtocol

        _protocol_check: ClassVar[ApiProtocol] = cast("CrawlerAPI", None)

    log: logging.Logger
    crawler: Crawler
    metadata: ClassVar[ApiMetadata] = ApiMetadata()

    def __init__(self, crawler: Crawler) -> None:
        self.log = logging.getLogger(__name__)
        self.crawler = crawler

    @property
    def server(self) -> ChiaServer:
        assert self.crawler.server is not None
        return self.crawler.server

    def ready(self) -> bool:
        return True

    @metadata.request(peer_required=True)
    async def request_peers(
        self, _request: full_node_protocol.RequestPeers, peer: WSChiaConnection
    ) -> Optional[Message]:
        pass

    @metadata.request(peer_required=True)
    async def respond_peers(
        self, request: full_node_protocol.RespondPeers, peer: WSChiaConnection
    ) -> Optional[Message]:
        pass

    @metadata.request(peer_required=True)
    async def new_peak(self, request: full_node_protocol.NewPeak, peer: WSChiaConnection) -> Optional[Message]:
        await self.crawler.new_peak(request, peer)
        return None

    @metadata.request()
    async def new_transaction(self, transaction: full_node_protocol.NewTransaction) -> Optional[Message]:
        pass

    @metadata.request(peer_required=True)
    async def new_signage_point_or_end_of_sub_slot(
        self, new_sp: full_node_protocol.NewSignagePointOrEndOfSubSlot, peer: WSChiaConnection
    ) -> Optional[Message]:
        pass

    @metadata.request()
    async def new_unfinished_block(
        self, new_unfinished_block: full_node_protocol.NewUnfinishedBlock
    ) -> Optional[Message]:
        pass

    @metadata.request()
    async def new_unfinished_block2(
        self, new_unfinished_block: full_node_protocol.NewUnfinishedBlock2
    ) -> Optional[Message]:
        pass

    @metadata.request(peer_required=True)
    async def new_compact_vdf(
        self, request: full_node_protocol.NewCompactVDF, peer: WSChiaConnection
    ) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_transaction(self, request: full_node_protocol.RequestTransaction) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_proof_of_weight(self, request: full_node_protocol.RequestProofOfWeight) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_block(self, request: full_node_protocol.RequestBlock) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_blocks(self, request: full_node_protocol.RequestBlocks) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_unfinished_block(
        self, request_unfinished_block: full_node_protocol.RequestUnfinishedBlock
    ) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_signage_point_or_end_of_sub_slot(
        self, request: full_node_protocol.RequestSignagePointOrEndOfSubSlot
    ) -> Optional[Message]:
        pass

    @metadata.request(peer_required=True)
    async def request_mempool_transactions(
        self,
        request: full_node_protocol.RequestMempoolTransactions,
        peer: WSChiaConnection,
    ) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_block_header(self, request: wallet_protocol.RequestBlockHeader) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_additions(self, request: wallet_protocol.RequestAdditions) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_removals(self, request: wallet_protocol.RequestRemovals) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_puzzle_solution(self, request: wallet_protocol.RequestPuzzleSolution) -> Optional[Message]:
        pass

    @metadata.request()
    async def request_header_blocks(self, request: wallet_protocol.RequestHeaderBlocks) -> Optional[Message]:
        pass
