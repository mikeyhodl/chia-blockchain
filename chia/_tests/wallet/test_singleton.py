from __future__ import annotations

import pytest
from chia_rs.sized_bytes import bytes32
from clvm_tools import binutils

from chia.consensus.condition_tools import parse_sexp_to_conditions
from chia.types.blockchain_format.program import INFINITE_COST, Program
from chia.wallet.conditions import AssertPuzzleAnnouncement
from chia.wallet.puzzles.singleton_top_layer import P2_SINGLETON_MOD, SINGLETON_MOD, SINGLETON_MOD_HASH
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH as LAUNCHER_PUZZLE_HASH

LAUNCHER_ID = Program.to(b"launcher-id").get_tree_hash()
POOL_REWARD_PREFIX_MAINNET = bytes32.fromhex("ccd5bb71183532bff220ba46c268991a00000000000000000000000000000000")


def singleton_puzzle(launcher_id: bytes32, launcher_puzzle_hash: bytes32, inner_puzzle: Program) -> Program:
    return SINGLETON_MOD.curry((SINGLETON_MOD_HASH, (launcher_id, launcher_puzzle_hash)), inner_puzzle)


def p2_singleton_puzzle(launcher_id: bytes32, launcher_puzzle_hash: bytes32) -> Program:
    return P2_SINGLETON_MOD.curry(SINGLETON_MOD_HASH, launcher_id, launcher_puzzle_hash)


def singleton_puzzle_hash(launcher_id: bytes32, launcher_puzzle_hash: bytes32, inner_puzzle: Program) -> bytes32:
    return singleton_puzzle(launcher_id, launcher_puzzle_hash, inner_puzzle).get_tree_hash()


def p2_singleton_puzzle_hash(launcher_id: bytes32, launcher_puzzle_hash: bytes32) -> bytes32:
    return p2_singleton_puzzle(launcher_id, launcher_puzzle_hash).get_tree_hash()


def test_only_odd_coins() -> None:
    singleton_mod_hash = SINGLETON_MOD.get_tree_hash()
    # (SINGLETON_STRUCT INNER_PUZZLE lineage_proof my_amount inner_solution)
    # SINGLETON_STRUCT = (MOD_HASH . (LAUNCHER_ID . LAUNCHER_PUZZLE_HASH))
    solution = Program.to(
        [
            (singleton_mod_hash, (LAUNCHER_ID, LAUNCHER_PUZZLE_HASH)),
            Program.to(binutils.assemble("(q (51 0xcafef00d 200))")),
            [0xDEADBEEF, 0xCAFEF00D, 200],
            200,
            [],
        ]
    )

    with pytest.raises(Exception) as exception_info:
        SINGLETON_MOD.run_with_cost(INFINITE_COST, solution)
    assert exception_info.value.args == ("clvm raise", "80")

    solution = Program.to(
        [
            (singleton_mod_hash, (LAUNCHER_ID, LAUNCHER_PUZZLE_HASH)),
            Program.to(binutils.assemble("(q (51 0xcafef00d 201))")),
            [0xDEADBEEF, 0xCAFED00D, 210],
            205,
            0,
        ]
    )
    SINGLETON_MOD.run_with_cost(INFINITE_COST, solution)


def test_only_one_odd_coin_created() -> None:
    singleton_mod_hash = SINGLETON_MOD.get_tree_hash()
    clsp = "(q (51 0xcafef00d 203) (51 0xfadeddab 205))"
    solution = Program.to(
        [
            (singleton_mod_hash, (LAUNCHER_ID, LAUNCHER_PUZZLE_HASH)),
            Program.to(binutils.assemble(clsp)),
            [0xDEADBEEF, 0xCAFEF00D, 411],
            411,
            [],
        ]
    )

    with pytest.raises(Exception) as exception_info:
        SINGLETON_MOD.run_with_cost(INFINITE_COST, solution)
    assert exception_info.value.args == ("clvm raise", "80")
    clsp = "(q (51 0xcafef00d 203) (51 0xfadeddab 204) (51 0xdeadbeef 202))"
    solution = Program.to(
        [
            (singleton_mod_hash, (LAUNCHER_ID, LAUNCHER_PUZZLE_HASH)),
            Program.to(binutils.assemble(clsp)),
            [0xDEADBEEF, 0xCAFEF00D, 411],
            411,
            [],
        ]
    )
    SINGLETON_MOD.run_with_cost(INFINITE_COST, solution)


def test_p2_singleton() -> None:
    # create a singleton. This should call driver code.
    launcher_id = LAUNCHER_ID
    innerpuz = Program.to(1)
    singleton_full_puzzle = singleton_puzzle(launcher_id, LAUNCHER_PUZZLE_HASH, innerpuz)

    # create a fake coin id for the `p2_singleton`
    p2_singleton_coin_id = Program.to(["test_hash"]).get_tree_hash()
    expected_announcement = AssertPuzzleAnnouncement(
        asserted_ph=singleton_full_puzzle.get_tree_hash(), asserted_msg=p2_singleton_coin_id
    ).msg_calc

    # create a `p2_singleton` puzzle. This should call driver code.
    p2_singleton_full = p2_singleton_puzzle(launcher_id, LAUNCHER_PUZZLE_HASH)
    solution = Program.to([innerpuz.get_tree_hash(), p2_singleton_coin_id])
    _, result = p2_singleton_full.run_with_cost(INFINITE_COST, solution)
    conditions = parse_sexp_to_conditions(result)

    p2_singleton_full = p2_singleton_puzzle(launcher_id, LAUNCHER_PUZZLE_HASH)
    solution = Program.to([innerpuz.get_tree_hash(), p2_singleton_coin_id])
    _, result = p2_singleton_full.run_with_cost(INFINITE_COST, solution)
    assert result.first().rest().first().as_atom() == expected_announcement
    assert conditions[0].vars[0] == expected_announcement
