from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from chia_rs import CoinSpend, CoinState
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint16
from typing_extensions import Self

from chia.types.blockchain_format.program import Program
from chia.util.streamable import Streamable, streamable
from chia.wallet.nft_wallet.nft_puzzles import NFT_OWNERSHIP_LAYER
from chia.wallet.nft_wallet.nft_puzzles import NFT_STATE_LAYER_MOD as NFT_MOD
from chia.wallet.singleton import SINGLETON_TOP_LAYER_MOD

log = logging.getLogger(__name__)


@streamable
@dataclass(frozen=True)
class UncurriedNFT(Streamable):
    """
    A simple solution for uncurry NFT puzzle.
    Initial the class with a full NFT puzzle, it will do a deep uncurry.
    This is the only place you need to change after modified the Chialisp curried parameters.
    """

    nft_mod_hash: bytes32
    """NFT module hash"""

    nft_state_layer: Program
    """NFT state layer puzzle"""

    singleton_struct: Program
    """
    Singleton struct
    [singleton_mod_hash, singleton_launcher_id, launcher_puzhash]
    """
    singleton_mod_hash: Program
    singleton_launcher_id: bytes32
    launcher_puzhash: Program

    metadata_updater_hash: Program
    """Metadata updater puzzle hash"""

    metadata: Program
    """
    NFT metadata
    [("u", data_uris), ("h", data_hash)]
    """
    data_uris: Program
    data_hash: Program
    meta_uris: Program
    meta_hash: Program
    license_uris: Program
    license_hash: Program
    edition_number: Program
    edition_total: Program

    inner_puzzle: Program
    """NFT state layer inner puzzle"""

    p2_puzzle: Program
    """p2 puzzle of the owner, either for ownership layer or standard"""

    # ownership layer fields
    owner_did: Optional[bytes32]
    """Owner's DID"""

    supports_did: bool
    """If the inner puzzle support the DID"""

    nft_inner_puzzle_hash: Optional[bytes32]
    """Puzzle hash of the ownership layer inner puzzle """

    transfer_program: Optional[Program]
    """Puzzle hash of the transfer program"""

    transfer_program_curry_params: Optional[Program]
    """
    Curried parameters of the transfer program
    [royalty_address, trade_price_percentage, settlement_mod_hash, cat_mod_hash]
    """
    royalty_address: Optional[bytes32]
    trade_price_percentage: Optional[uint16]

    @classmethod
    def uncurry(cls, mod: Program, curried_args: Program) -> Optional[Self]:
        """
        Try to uncurry a NFT puzzle
        :param cls UncurriedNFT class
        :param mod: uncurried Puzzle program
        :param uncurried_args: uncurried arguments to program
        :return Uncurried NFT
        """
        if mod != SINGLETON_TOP_LAYER_MOD:
            log.debug("Cannot uncurry NFT puzzle, failed on singleton top layer: Mod %s", mod)
            return None
        try:
            (singleton_struct, nft_state_layer) = curried_args.as_iter()
            singleton_mod_hash = singleton_struct.first()
            singleton_launcher_id = singleton_struct.rest().first()
            launcher_puzhash = singleton_struct.rest().rest()
        except ValueError as e:
            log.debug("Cannot uncurry singleton top layer: Args %s error: %s", curried_args, e)
            return None

        mod, curried_args = curried_args.rest().first().uncurry()
        if mod != NFT_MOD:
            log.debug("Cannot uncurry NFT puzzle, failed on NFT state layer: Mod %s", mod)
            return None
        try:
            # Set nft parameters
            nft_mod_hash, metadata, metadata_updater_hash, inner_puzzle = curried_args.as_iter()
            data_uris = Program.to([])
            data_hash = Program.to(0)
            meta_uris = Program.to([])
            meta_hash = Program.to(0)
            license_uris = Program.to([])
            license_hash = Program.to(0)
            edition_number = Program.to(1)
            edition_total = Program.to(1)
            # Set metadata
            for kv_pair in metadata.as_iter():
                if kv_pair.first().as_atom() == b"u":
                    data_uris = kv_pair.rest()
                if kv_pair.first().as_atom() == b"h":
                    data_hash = kv_pair.rest()
                if kv_pair.first().as_atom() == b"mu":
                    meta_uris = kv_pair.rest()
                if kv_pair.first().as_atom() == b"mh":
                    meta_hash = kv_pair.rest()
                if kv_pair.first().as_atom() == b"lu":
                    license_uris = kv_pair.rest()
                if kv_pair.first().as_atom() == b"lh":
                    license_hash = kv_pair.rest()
                if kv_pair.first().as_atom() == b"sn":
                    edition_number = kv_pair.rest()
                if kv_pair.first().as_atom() == b"st":
                    edition_total = kv_pair.rest()
            current_did: Optional[bytes32] = None
            transfer_program = None
            transfer_program_args = None
            royalty_address: Optional[bytes32] = None
            royalty_percentage: Optional[uint16] = None
            nft_inner_puzzle_mod = None
            mod, ol_args = inner_puzzle.uncurry()
            supports_did = False
            if mod == NFT_OWNERSHIP_LAYER:
                supports_did = True
                log.debug("Parsing ownership layer")
                _, current_did_p, transfer_program, p2_puzzle = ol_args.as_iter()
                _, transfer_program_args = transfer_program.uncurry()
                _, royalty_address_p, royalty_percentage_p = transfer_program_args.as_iter()
                royalty_percentage = uint16(royalty_percentage_p.as_int())
                royalty_address = bytes32(royalty_address_p.as_atom())
                current_did_atom = current_did_p.as_atom()
                if current_did_atom == b"":
                    # For unassigned NFT, set owner DID to None
                    current_did = None
                else:
                    current_did = bytes32(current_did_atom)
            else:
                log.debug("Creating a standard NFT puzzle")
                p2_puzzle = inner_puzzle
        except Exception as e:
            log.debug("Cannot uncurry NFT state layer: Args %s Error: %s", curried_args, e)
            return None

        return cls(
            nft_mod_hash=bytes32(nft_mod_hash.as_atom()),
            nft_state_layer=nft_state_layer,
            singleton_struct=singleton_struct,
            singleton_mod_hash=singleton_mod_hash,
            singleton_launcher_id=bytes32(singleton_launcher_id.as_atom()),
            launcher_puzhash=launcher_puzhash,
            metadata=metadata,
            data_uris=data_uris,
            data_hash=data_hash,
            p2_puzzle=p2_puzzle,
            metadata_updater_hash=metadata_updater_hash,
            meta_uris=meta_uris,
            meta_hash=meta_hash,
            license_uris=license_uris,
            license_hash=license_hash,
            edition_number=edition_number,
            edition_total=edition_total,
            inner_puzzle=inner_puzzle,
            owner_did=current_did,
            supports_did=supports_did,
            transfer_program=transfer_program,
            transfer_program_curry_params=transfer_program_args,
            royalty_address=royalty_address,
            trade_price_percentage=royalty_percentage,
            nft_inner_puzzle_hash=nft_inner_puzzle_mod,
        )

    def get_innermost_solution(self, solution: Program) -> Program:
        state_layer_inner_solution: Program = solution.at("rrff")
        if self.supports_did:
            return state_layer_inner_solution.first()
        else:
            return state_layer_inner_solution


@streamable
@dataclass(frozen=True)
class NFTCoinData(Streamable):
    uncurried_nft: UncurriedNFT
    parent_coin_state: CoinState
    parent_coin_spend: CoinSpend
