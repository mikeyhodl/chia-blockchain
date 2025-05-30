from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar, cast, get_args, get_origin

from chia_rs import CoinSpend, G1Element, G2Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from clvm_tools import binutils

from chia._tests.clvm.coin_store import BadSpendBundleError, CoinStore, CoinTimestamp
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.pools.pool_puzzles import POOL_MEMBER_MOD
from chia.pools.pool_puzzles import POOL_WAITING_ROOM_MOD as POOL_WAITINGROOM_MOD
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.conditions import AssertCoinAnnouncement
from chia.wallet.puzzles.singleton_top_layer import (
    P2_SINGLETON_OR_DELAYED_MOD as P2_SINGLETON_MOD,
)
from chia.wallet.puzzles.singleton_top_layer import P2_SINGLETON_OR_DELAYED_MOD_HASH as P2_SINGLETON_MOD_HASH
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER, SINGLETON_MOD, SINGLETON_MOD_HASH
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_LAUNCHER_HASH as LAUNCHER_PUZZLE_HASH
from chia.wallet.util.compute_additions import compute_additions
from chia.wallet.util.debug_spend_bundle import debug_spend_bundle
from chia.wallet.wallet_spend_bundle import WalletSpendBundle

LAUNCHER_PUZZLE = SINGLETON_LAUNCHER

ANYONE_CAN_SPEND_PUZZLE = Program.to(1)
ANYONE_CAN_SPEND_WITH_PADDING_PUZZLE_HASH = Program.to(binutils.assemble("(a (q . 1) 3)")).get_tree_hash()

POOL_REWARD_PREFIX_MAINNET = bytes32.fromhex("ccd5bb71183532bff220ba46c268991a00000000000000000000000000000000")

MAX_BLOCK_COST_CLVM = 10**18

T = TypeVar("T")


def satisfies_hint(obj: T, type_hint: type[T]) -> bool:
    """
    Check if an object satisfies a type hint.
    This is a simplified version of `isinstance` that also handles generic types.
    """
    # Start from the initial type hint
    object_hint_pairs = [(obj, type_hint)]
    while len(object_hint_pairs) > 0:
        obj, type_hint = object_hint_pairs.pop()
        origin = get_origin(type_hint)
        args = get_args(type_hint)
        if origin:
            # Handle generic types
            if not isinstance(obj, origin):
                return False
            if len(args) > 0:
                # tuple[T, ...] gets handled just like list[T]
                if origin is list or (origin is tuple and args[-1] is Ellipsis):
                    object_hint_pairs.extend((item, args[0]) for item in obj)
                elif origin is tuple:
                    object_hint_pairs.extend((item, arg) for item, arg in zip(obj, args))
                elif origin is dict:
                    object_hint_pairs.extend((k, args[0]) for k in obj.keys())
                    object_hint_pairs.extend((v, args[1]) for v in obj.values())
                else:
                    raise NotImplementedError(f"Type {origin} is not yet supported")
        else:
            # Handle concrete types
            if type(obj) is not type_hint:
                return False
    return True


class PuzzleDB:
    def __init__(self) -> None:
        self._db: dict[bytes32, Program] = {}

    def add_puzzle(self, puzzle: Program) -> None:
        self._db[puzzle.get_tree_hash()] = Program.from_bytes(bytes(puzzle))

    def puzzle_for_hash(self, puzzle_hash: bytes32) -> Optional[Program]:
        return self._db.get(puzzle_hash)


def from_kwargs(kwargs: dict[str, Any], key: str, type_info: type[T]) -> T:
    """Raise an exception if `kwargs[key]` is missing or the wrong type"""
    if key not in kwargs:
        raise ValueError(f"`{key}` missing in call to `solve`")
    if not satisfies_hint(kwargs[key], type_info):
        raise TypeError(f"`{key}` must be of type {type_info} but is of type {type(kwargs[key])}")
    return cast(T, kwargs[key])


Solver_F = Callable[["Solver", PuzzleDB, list[Program], Any], Program]


class Solver:
    """
    This class registers puzzle templates by hash and solves them.
    """

    def __init__(self) -> None:
        self.solvers_by_puzzle_hash: dict[bytes32, Solver_F] = {}

    def register_solver(self, puzzle_hash: bytes32, solver_f: Solver_F) -> None:
        if puzzle_hash in self.solvers_by_puzzle_hash:
            raise ValueError(f"solver registered for {puzzle_hash}")
        self.solvers_by_puzzle_hash[puzzle_hash] = solver_f

    def solve(self, puzzle_db: PuzzleDB, puzzle: Program, **kwargs: dict[str, Any]) -> Program:
        """
        The legal values and types for `kwargs` depends on the underlying solver
        that's invoked. The `kwargs` are passed through to any inner solvers
        that may need to be called.
        """
        puzzle_hash = puzzle.get_tree_hash()
        puzzle_args = []
        if puzzle_hash not in self.solvers_by_puzzle_hash:
            puzzle_template, args = puzzle.uncurry()
            puzzle_args = list(args.as_iter())
            puzzle_hash = puzzle_template.get_tree_hash()
        solver_f = self.solvers_by_puzzle_hash.get(puzzle_hash)
        if solver_f:
            return solver_f(self, puzzle_db, puzzle_args, kwargs)

        raise ValueError("can't solve")


def solve_launcher(solver: Solver, puzzle_db: PuzzleDB, args: list[Program], kwargs: dict[str, Any]) -> Program:
    launcher_amount = from_kwargs(kwargs, "launcher_amount", uint64)
    destination_puzzle_hash = from_kwargs(kwargs, "destination_puzzle_hash", bytes32)
    metadata = from_kwargs(kwargs, "metadata", list[tuple[str, str]])
    solution = Program.to([destination_puzzle_hash, launcher_amount, metadata])
    return solution


def solve_anyone_can_spend(solver: Solver, puzzle_db: PuzzleDB, args: list[Program], kwargs: dict[str, Any]) -> Program:
    """
    This is the anyone-can-spend puzzle `1`. Note that farmers can easily steal this coin, so don't use
    it except for testing.
    """
    conditions = from_kwargs(kwargs, "conditions", list[Program])
    solution = Program.to(conditions)
    return solution


def solve_anyone_can_spend_with_padding(
    solver: Solver, puzzle_db: PuzzleDB, args: list[Program], kwargs: dict[str, Any]
) -> Program:
    """This is the puzzle `(a (q . 1) 3)`. It's only for testing."""
    conditions = from_kwargs(kwargs, "conditions", list[Program])
    solution = Program.to((0, conditions))
    return solution


def solve_singleton(solver: Solver, puzzle_db: PuzzleDB, args: list[Program], kwargs: dict[str, Any]) -> Program:
    """
    `lineage_proof`: a `Program` that proves the parent is also a singleton (or the launcher).
    `coin_amount`: a necessarily-odd value of mojos in this coin.
    """
    _, inner_puzzle = args
    inner_solution = solver.solve(puzzle_db, inner_puzzle, **kwargs)
    lineage_proof = from_kwargs(kwargs, "lineage_proof", Program)
    coin_amount = from_kwargs(kwargs, "coin_amount", uint64)
    solution = inner_solution.to([lineage_proof, coin_amount, inner_solution.rest()])
    return solution


def solve_pool_member(solver: Solver, puzzle_db: PuzzleDB, args: list[Program], kwargs: dict[str, Any]) -> Program:
    pool_member_spend_type = from_kwargs(kwargs, "pool_member_spend_type", str)
    allowable = ["to-waiting-room", "claim-p2-nft"]
    if pool_member_spend_type not in allowable:
        raise ValueError(f"`pool_member_spend_type` must be one of {'/'.join(allowable)} for POOL_MEMBER puzzle")
    to_waiting_room = pool_member_spend_type == "to-waiting-room"
    if to_waiting_room:
        key_value_list = from_kwargs(kwargs, "key_value_list", Program)
        return Program.to([0, 1, 0, 0, key_value_list])
    # it's an "absorb_pool_reward" type
    pool_reward_amount = from_kwargs(kwargs, "pool_reward_amount", uint64)
    pool_reward_height = from_kwargs(kwargs, "pool_reward_height", int)
    solution = Program.to([0, pool_reward_amount, pool_reward_height])
    return solution


def solve_pool_waiting_room(
    solver: Solver, puzzle_db: PuzzleDB, args: list[Program], kwargs: dict[str, Any]
) -> Program:
    pool_leaving_spend_type = from_kwargs(kwargs, "pool_leaving_spend_type", str)
    allowable = ["exit-waiting-room", "claim-p2-nft"]
    if pool_leaving_spend_type not in allowable:
        raise ValueError(f"`pool_leaving_spend_type` must be one of {'/'.join(allowable)} for POOL_MEMBER puzzle")
    exit_waiting_room = pool_leaving_spend_type == "exit-waiting-room"
    if exit_waiting_room:
        key_value_list = from_kwargs(kwargs, "key_value_list", list[tuple[str, str]])
        destination_puzzle_hash = from_kwargs(kwargs, "destination_puzzle_hash", bytes32)
        return Program.to([0, 1, key_value_list, destination_puzzle_hash])
    # it's an "absorb_pool_reward" type
    pool_reward_amount = from_kwargs(kwargs, "pool_reward_amount", uint64)
    pool_reward_height = from_kwargs(kwargs, "pool_reward_height", int)
    solution = Program.to([0, 0, pool_reward_amount, pool_reward_height])
    return solution


def solve_p2_singleton(solver: Solver, puzzle_db: PuzzleDB, args: list[Program], kwargs: dict[str, Any]) -> Program:
    p2_singleton_spend_type = from_kwargs(kwargs, "p2_singleton_spend_type", str)
    allowable = ["claim-p2-nft", "delayed-spend"]
    if p2_singleton_spend_type not in allowable:
        raise ValueError(f"`p2_singleton_spend_type` must be one of {'/'.join(allowable)} for P2_SINGLETON puzzle")
    claim_p2_nft = p2_singleton_spend_type == "claim-p2-nft"
    if claim_p2_nft:
        singleton_inner_puzzle_hash = from_kwargs(kwargs, "singleton_inner_puzzle_hash", bytes32)
        p2_singleton_coin_name = from_kwargs(kwargs, "p2_singleton_coin_name", bytes32)
        solution = Program.to([singleton_inner_puzzle_hash, p2_singleton_coin_name])
        return solution
    raise ValueError("can't solve `delayed-spend` yet")


SOLVER = Solver()
SOLVER.register_solver(LAUNCHER_PUZZLE_HASH, solve_launcher)
SOLVER.register_solver(ANYONE_CAN_SPEND_WITH_PADDING_PUZZLE_HASH, solve_anyone_can_spend_with_padding)
SOLVER.register_solver(SINGLETON_MOD_HASH, solve_singleton)
SOLVER.register_solver(POOL_MEMBER_MOD.get_tree_hash(), solve_pool_member)
SOLVER.register_solver(POOL_WAITINGROOM_MOD.get_tree_hash(), solve_pool_waiting_room)
SOLVER.register_solver(ANYONE_CAN_SPEND_PUZZLE.get_tree_hash(), solve_anyone_can_spend)
SOLVER.register_solver(P2_SINGLETON_MOD_HASH, solve_p2_singleton)


def solve_puzzle(puzzle_db: PuzzleDB, puzzle: Program, **kwargs: Any) -> Program:
    return SOLVER.solve(puzzle_db, puzzle, **kwargs)


@dataclass
class SingletonWallet:
    launcher_id: bytes32
    launcher_puzzle_hash: bytes32
    key_value_list: Program
    current_state: Coin
    lineage_proof: Program

    def inner_puzzle(self, puzzle_db: PuzzleDB) -> Optional[Program]:
        puzzle = puzzle_db.puzzle_for_hash(self.current_state.puzzle_hash)
        if puzzle is None:
            return None
        return self.inner_puzzle_for_puzzle(puzzle)

    def inner_puzzle_for_puzzle(self, puzzle: Program) -> Program:
        assert puzzle.get_tree_hash() == self.current_state.puzzle_hash
        template, args = puzzle.uncurry()
        assert bytes(template) == bytes(SINGLETON_MOD)
        _, inner_puzzle = list(args.as_iter())
        return inner_puzzle

    def coin_spend_for_conditions(self, puzzle_db: PuzzleDB, **kwargs: object) -> CoinSpend:
        coin = self.current_state
        puzzle_reveal = puzzle_db.puzzle_for_hash(coin.puzzle_hash)
        assert puzzle_reveal is not None
        solution = solve_puzzle(
            puzzle_db, puzzle_reveal, lineage_proof=self.lineage_proof, coin_amount=coin.amount, **kwargs
        )
        return make_spend(coin, puzzle_reveal, solution)

    def update_state(self, puzzle_db: PuzzleDB, removals: list[CoinSpend]) -> int:
        state_change_count = 0
        current_coin_name = self.current_state.name()
        for coin_spend in removals:
            if coin_spend.coin.name() == current_coin_name:
                for coin in compute_additions(coin_spend):
                    if coin.amount & 1 == 1:
                        parent_puzzle_hash = coin_spend.coin.puzzle_hash
                        parent_puzzle = puzzle_db.puzzle_for_hash(parent_puzzle_hash)
                        assert parent_puzzle is not None
                        parent_inner_puzzle = self.inner_puzzle_for_puzzle(parent_puzzle)
                        assert parent_inner_puzzle is not None
                        parent_inner_puzzle_hash = parent_inner_puzzle.get_tree_hash()
                        lineage_proof = Program.to(
                            [self.current_state.parent_coin_info, parent_inner_puzzle_hash, coin.amount]
                        )
                        self.lineage_proof = lineage_proof
                        self.current_state = coin
                        state_change_count += 1
        return state_change_count


def adaptor_for_singleton_inner_puzzle(puzzle: Program) -> Program:
    """
    The singleton puzzle requires an inner puzzle which gets passed some "truths" from
    the singleton that are guaranteed to be correct. Using these truths may reduce the
    size of the inner puzzle, since any values can be used knowing they are checked elsewhere.
    However, an inner puzzle that is not aware that this first argument contains these
    values can be "adapted" using this function to ignore the first argument (and slide
    the subsequent arguments over), allowing any inner puzzle that thinks it's an outer
    puzzle to work as a singleton inner puzzle.
    """
    # this is pretty slow and lame
    program = binutils.assemble(f"(a (q . {binutils.disassemble(puzzle)}) 3)")
    return Program.to(program)


def launcher_conditions_and_spend_bundle(
    puzzle_db: PuzzleDB,
    parent_coin_id: bytes32,
    launcher_amount: uint64,
    initial_singleton_inner_puzzle: Program,
    metadata: list[tuple[str, str]],
    launcher_puzzle: Program,
) -> tuple[bytes32, list[Program], WalletSpendBundle]:
    puzzle_db.add_puzzle(launcher_puzzle)
    launcher_puzzle_hash = launcher_puzzle.get_tree_hash()
    launcher_coin = Coin(parent_coin_id, launcher_puzzle_hash, launcher_amount)
    launcher_id = launcher_coin.name()
    singleton_full_puzzle = singleton_puzzle(launcher_id, launcher_puzzle_hash, initial_singleton_inner_puzzle)
    puzzle_db.add_puzzle(singleton_full_puzzle)
    singleton_full_puzzle_hash = singleton_full_puzzle.get_tree_hash()
    message_program = Program.to([singleton_full_puzzle_hash, launcher_amount, metadata])
    expected_announcement = AssertCoinAnnouncement(
        asserted_id=launcher_id, asserted_msg=message_program.get_tree_hash()
    )
    expected_conditions = []
    clsp = f"(0x{ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT.hex()} 0x{expected_announcement.msg_calc})"
    expected_conditions.append(Program.to(binutils.assemble(clsp)))
    clsp = f"(0x{ConditionOpcode.CREATE_COIN.hex()} 0x{launcher_puzzle_hash} {launcher_amount})"
    expected_conditions.append(Program.to(binutils.assemble(clsp)))
    solution = solve_puzzle(
        puzzle_db,
        launcher_puzzle,
        destination_puzzle_hash=singleton_full_puzzle_hash,
        launcher_amount=launcher_amount,
        metadata=metadata,
    )
    coin_spend = make_spend(launcher_coin, launcher_puzzle, solution)
    spend_bundle = WalletSpendBundle([coin_spend], G2Element())
    return launcher_id, expected_conditions, spend_bundle


def singleton_puzzle(launcher_id: bytes32, launcher_puzzle_hash: bytes32, inner_puzzle: Program) -> Program:
    return SINGLETON_MOD.curry((SINGLETON_MOD_HASH, (launcher_id, launcher_puzzle_hash)), inner_puzzle)


def singleton_puzzle_hash(launcher_id: bytes32, launcher_puzzle_hash: bytes32, inner_puzzle: Program) -> bytes32:
    return singleton_puzzle(launcher_id, launcher_puzzle_hash, inner_puzzle).get_tree_hash()


def solution_for_singleton_puzzle(lineage_proof: Program, my_amount: int, inner_solution: Program) -> Program:
    return Program.to([lineage_proof, my_amount, inner_solution])


def p2_singleton_puzzle_for_launcher(
    puzzle_db: PuzzleDB,
    launcher_id: bytes32,
    launcher_puzzle_hash: bytes32,
    seconds_delay: int,
    delayed_puzzle_hash: bytes32,
) -> Program:
    puzzle = P2_SINGLETON_MOD.curry(
        SINGLETON_MOD_HASH, launcher_id, launcher_puzzle_hash, seconds_delay, delayed_puzzle_hash
    )
    puzzle_db.add_puzzle(puzzle)
    return puzzle


def p2_singleton_puzzle_hash_for_launcher(
    puzzle_db: PuzzleDB,
    launcher_id: bytes32,
    launcher_puzzle_hash: bytes32,
    seconds_delay: int,
    delayed_puzzle_hash: bytes32,
) -> bytes32:
    return p2_singleton_puzzle_for_launcher(
        puzzle_db, launcher_id, launcher_puzzle_hash, seconds_delay, delayed_puzzle_hash
    ).get_tree_hash()


def claim_p2_singleton(
    puzzle_db: PuzzleDB, singleton_wallet: SingletonWallet, p2_singleton_coin: Coin
) -> tuple[CoinSpend, list[Program]]:
    inner_puzzle = singleton_wallet.inner_puzzle(puzzle_db)
    assert inner_puzzle
    inner_puzzle_hash = inner_puzzle.get_tree_hash()
    p2_singleton_puzzle = puzzle_db.puzzle_for_hash(p2_singleton_coin.puzzle_hash)
    assert p2_singleton_puzzle is not None
    p2_singleton_coin_name = p2_singleton_coin.name()
    p2_singleton_solution = solve_puzzle(
        puzzle_db,
        p2_singleton_puzzle,
        p2_singleton_spend_type="claim-p2-nft",
        singleton_inner_puzzle_hash=inner_puzzle_hash,
        p2_singleton_coin_name=bytes32(p2_singleton_coin_name),
    )
    p2_singleton_coin_spend = make_spend(p2_singleton_coin, p2_singleton_puzzle, p2_singleton_solution)
    expected_p2_singleton_announcement = AssertCoinAnnouncement(
        asserted_id=p2_singleton_coin_name, asserted_msg=b"$"
    ).msg_calc
    singleton_conditions = [
        Program.to([ConditionOpcode.CREATE_PUZZLE_ANNOUNCEMENT, p2_singleton_coin_name]),
        Program.to([ConditionOpcode.CREATE_COIN, inner_puzzle_hash, 1]),
        Program.to([ConditionOpcode.ASSERT_COIN_ANNOUNCEMENT, expected_p2_singleton_announcement]),
    ]
    return p2_singleton_coin_spend, singleton_conditions


def lineage_proof_for_coin_spend(coin_spend: CoinSpend) -> Program:
    """Take a coin solution, return a lineage proof for their child to use in spends"""
    coin = coin_spend.coin
    parent_name = coin.parent_coin_info
    amount = coin.amount

    inner_puzzle_hash = None
    if coin.puzzle_hash == LAUNCHER_PUZZLE_HASH:
        return Program.to([parent_name, amount])

    full_puzzle = Program.from_bytes(bytes(coin_spend.puzzle_reveal))
    _, args = full_puzzle.uncurry()
    _, __, ___, inner_puzzle = list(args.as_iter())
    inner_puzzle_hash = inner_puzzle.get_tree_hash()

    return Program.to([parent_name, inner_puzzle_hash, amount])


def create_throwaway_pubkey(seed: bytes) -> G1Element:
    return G1Element.generator()


def assert_coin_spent(coin_store: CoinStore, coin: Coin, is_spent: bool = True) -> None:
    coin_record = coin_store.coin_record(coin.name())
    assert coin_record is not None
    assert coin_record.spent is is_spent


def spend_coin_to_singleton(
    puzzle_db: PuzzleDB, launcher_puzzle: Program, coin_store: CoinStore, now: CoinTimestamp
) -> tuple[list[Coin], list[CoinSpend]]:
    farmed_coin_amount = 100000
    metadata = [("foo", "bar")]

    now = CoinTimestamp(10012300, uint32(1))
    farmed_coin = coin_store.farm_coin(ANYONE_CAN_SPEND_PUZZLE.get_tree_hash(), now, amount=farmed_coin_amount)
    now.seconds += 500
    now.height = uint32(now.height + 1)

    launcher_amount: uint64 = uint64(1)
    launcher_puzzle = LAUNCHER_PUZZLE
    launcher_puzzle_hash = launcher_puzzle.get_tree_hash()
    initial_singleton_puzzle = adaptor_for_singleton_inner_puzzle(ANYONE_CAN_SPEND_PUZZLE)
    launcher_id, condition_list, launcher_spend_bundle = launcher_conditions_and_spend_bundle(
        puzzle_db, farmed_coin.name(), launcher_amount, initial_singleton_puzzle, metadata, launcher_puzzle
    )

    conditions = Program.to(condition_list)
    coin_spend = make_spend(farmed_coin, ANYONE_CAN_SPEND_PUZZLE, conditions)
    spend_bundle = WalletSpendBundle.aggregate([launcher_spend_bundle, WalletSpendBundle([coin_spend], G2Element())])

    additions, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)

    launcher_coin = launcher_spend_bundle.coin_spends[0].coin

    assert_coin_spent(coin_store, launcher_coin)
    assert_coin_spent(coin_store, farmed_coin)

    singleton_expected_puzzle = singleton_puzzle(launcher_id, launcher_puzzle_hash, initial_singleton_puzzle)
    singleton_expected_puzzle_hash = singleton_expected_puzzle.get_tree_hash()
    expected_singleton_coin = Coin(launcher_coin.name(), singleton_expected_puzzle_hash, launcher_amount)
    assert_coin_spent(coin_store, expected_singleton_coin, is_spent=False)

    return additions, removals


def find_interesting_singletons(removals: list[CoinSpend]) -> list[SingletonWallet]:
    singletons = []
    for coin_spend in removals:
        if coin_spend.coin.puzzle_hash == LAUNCHER_PUZZLE_HASH:
            r = Program.from_bytes(bytes(coin_spend.solution))
            key_value_list = r.rest().rest().first()
            eve_coin = compute_additions(coin_spend)[0]
            lineage_proof = lineage_proof_for_coin_spend(coin_spend)
            launcher_id = coin_spend.coin.name()
            singleton = SingletonWallet(
                launcher_id, coin_spend.coin.puzzle_hash, key_value_list, eve_coin, lineage_proof
            )
            singletons.append(singleton)
    return singletons


def filter_p2_singleton(puzzle_db: PuzzleDB, additions: list[Coin]) -> list[Coin]:
    r = []
    for coin in additions:
        puzzle = puzzle_db.puzzle_for_hash(coin.puzzle_hash)
        if puzzle is None:
            continue
        template, _ = puzzle.uncurry()
        if template.get_tree_hash() == P2_SINGLETON_MOD_HASH:
            r.append(coin)
    return r


def test_lifecycle_with_coinstore_as_wallet() -> None:
    PUZZLE_DB = PuzzleDB()

    interested_singletons = []

    #######
    # farm a coin

    coin_store = CoinStore(DEFAULT_CONSTANTS, int.from_bytes(POOL_REWARD_PREFIX_MAINNET, "big"))
    now = CoinTimestamp(10012300, uint32(1))

    DELAY_SECONDS = 86400
    DELAY_PUZZLE_HASH = bytes32.zeros

    #######
    # spend coin to a singleton

    _, removals = spend_coin_to_singleton(PUZZLE_DB, LAUNCHER_PUZZLE, coin_store, now)

    assert len(list(coin_store.all_unspent_coins())) == 1

    new_singletons = find_interesting_singletons(removals)
    interested_singletons.extend(new_singletons)

    assert len(interested_singletons) == 1

    SINGLETON_WALLET: SingletonWallet = interested_singletons[0]

    #######
    # farm a `p2_singleton`

    pool_reward_puzzle_hash = p2_singleton_puzzle_hash_for_launcher(
        PUZZLE_DB, SINGLETON_WALLET.launcher_id, SINGLETON_WALLET.launcher_puzzle_hash, DELAY_SECONDS, DELAY_PUZZLE_HASH
    )
    farmed_coin = coin_store.farm_coin(pool_reward_puzzle_hash, now)
    now.seconds += 500
    now.height = uint32(now.height + 1)

    p2_singleton_coins = filter_p2_singleton(PUZZLE_DB, [farmed_coin])
    assert p2_singleton_coins == [farmed_coin]

    assert len(list(coin_store.all_unspent_coins())) == 2

    #######
    # now collect the `p2_singleton` using the singleton

    for coin in p2_singleton_coins:
        p2_singleton_coin_spend, singleton_conditions = claim_p2_singleton(PUZZLE_DB, SINGLETON_WALLET, coin)

        coin_spend = SINGLETON_WALLET.coin_spend_for_conditions(PUZZLE_DB, conditions=singleton_conditions)
        spend_bundle = WalletSpendBundle([coin_spend, p2_singleton_coin_spend], G2Element())

        _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)
        now.seconds += 500
        now.height = uint32(now.height + 1)

        SINGLETON_WALLET.update_state(PUZZLE_DB, removals)

    assert len(list(coin_store.all_unspent_coins())) == 1

    #######
    # farm and collect another `p2_singleton`

    pool_reward_puzzle_hash = p2_singleton_puzzle_hash_for_launcher(
        PUZZLE_DB, SINGLETON_WALLET.launcher_id, SINGLETON_WALLET.launcher_puzzle_hash, DELAY_SECONDS, DELAY_PUZZLE_HASH
    )
    farmed_coin = coin_store.farm_coin(pool_reward_puzzle_hash, now)
    now.seconds += 500
    now.height = uint32(now.height + 1)

    p2_singleton_coins = filter_p2_singleton(PUZZLE_DB, [farmed_coin])
    assert p2_singleton_coins == [farmed_coin]

    assert len(list(coin_store.all_unspent_coins())) == 2

    for coin in p2_singleton_coins:
        p2_singleton_coin_spend, singleton_conditions = claim_p2_singleton(PUZZLE_DB, SINGLETON_WALLET, coin)

        coin_spend = SINGLETON_WALLET.coin_spend_for_conditions(PUZZLE_DB, conditions=singleton_conditions)
        spend_bundle = WalletSpendBundle([coin_spend, p2_singleton_coin_spend], G2Element())

        _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)
        now.seconds += 500
        now.height = uint32(now.height + 1)

        SINGLETON_WALLET.update_state(PUZZLE_DB, removals)

    assert len(list(coin_store.all_unspent_coins())) == 1

    #######
    # loan the singleton to a pool

    # puzzle_for_loan_singleton_to_pool(
    # pool_puzzle_hash, p2_singleton_puzzle_hash, owner_public_key, pool_reward_prefix, relative_lock_height)

    # calculate the series

    owner_public_key = bytes(create_throwaway_pubkey(b"foo"))
    pool_puzzle_hash = Program.to(bytes(create_throwaway_pubkey(b""))).get_tree_hash()
    pool_reward_prefix = POOL_REWARD_PREFIX_MAINNET
    relative_lock_height = 1440

    pool_escaping_puzzle = POOL_WAITINGROOM_MOD.curry(
        pool_puzzle_hash, pool_reward_puzzle_hash, owner_public_key, pool_reward_prefix, relative_lock_height
    )
    pool_escaping_puzzle_hash = pool_escaping_puzzle.get_tree_hash()

    pool_member_puzzle = POOL_MEMBER_MOD.curry(
        pool_puzzle_hash, pool_reward_puzzle_hash, owner_public_key, pool_reward_prefix, pool_escaping_puzzle_hash
    )
    pool_member_puzzle_hash = pool_member_puzzle.get_tree_hash()

    PUZZLE_DB.add_puzzle(pool_escaping_puzzle)
    PUZZLE_DB.add_puzzle(
        singleton_puzzle(SINGLETON_WALLET.launcher_id, SINGLETON_WALLET.launcher_puzzle_hash, pool_escaping_puzzle)
    )
    PUZZLE_DB.add_puzzle(pool_member_puzzle)
    full_puzzle = singleton_puzzle(
        SINGLETON_WALLET.launcher_id, SINGLETON_WALLET.launcher_puzzle_hash, pool_member_puzzle
    )
    PUZZLE_DB.add_puzzle(full_puzzle)

    conditions = [Program.to([ConditionOpcode.CREATE_COIN, pool_member_puzzle_hash, 1])]

    singleton_coin_spend = SINGLETON_WALLET.coin_spend_for_conditions(PUZZLE_DB, conditions=conditions)

    spend_bundle = WalletSpendBundle([singleton_coin_spend], G2Element())

    _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)

    assert len(list(coin_store.all_unspent_coins())) == 1

    SINGLETON_WALLET.update_state(PUZZLE_DB, removals)

    #######
    # farm a `p2_singleton`

    pool_reward_puzzle_hash = p2_singleton_puzzle_hash_for_launcher(
        PUZZLE_DB, SINGLETON_WALLET.launcher_id, SINGLETON_WALLET.launcher_puzzle_hash, DELAY_SECONDS, DELAY_PUZZLE_HASH
    )
    farmed_coin = coin_store.farm_coin(pool_reward_puzzle_hash, now)
    now.seconds += 500
    now.height = uint32(now.height + 1)

    p2_singleton_coins = filter_p2_singleton(PUZZLE_DB, [farmed_coin])
    assert p2_singleton_coins == [farmed_coin]

    assert len(list(coin_store.all_unspent_coins())) == 2

    #######
    # now collect the `p2_singleton` for the pool

    for coin in p2_singleton_coins:
        p2_singleton_coin_spend, singleton_conditions = claim_p2_singleton(PUZZLE_DB, SINGLETON_WALLET, coin)

        coin_spend = SINGLETON_WALLET.coin_spend_for_conditions(
            PUZZLE_DB,
            pool_member_spend_type="claim-p2-nft",
            pool_reward_amount=p2_singleton_coin_spend.coin.amount,
            pool_reward_height=now.height - 1,
        )
        spend_bundle = WalletSpendBundle([coin_spend, p2_singleton_coin_spend], G2Element())
        debug_spend_bundle(spend_bundle)

        _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)
        now.seconds += 500
        now.height = uint32(now.height + 1)

        SINGLETON_WALLET.update_state(PUZZLE_DB, removals)

    assert len(list(coin_store.all_unspent_coins())) == 2

    #######
    # spend the singleton into the "leaving the pool" state

    coin_spend = SINGLETON_WALLET.coin_spend_for_conditions(
        PUZZLE_DB, pool_member_spend_type="to-waiting-room", key_value_list=Program.to([("foo", "bar")])
    )
    spend_bundle = WalletSpendBundle([coin_spend], G2Element())

    _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)
    now.seconds += 500
    now.height = uint32(now.height + 1)
    change_count = SINGLETON_WALLET.update_state(PUZZLE_DB, removals)
    assert change_count == 1

    assert len(list(coin_store.all_unspent_coins())) == 2

    #######
    # farm a `p2_singleton`

    pool_reward_puzzle_hash = p2_singleton_puzzle_hash_for_launcher(
        PUZZLE_DB, SINGLETON_WALLET.launcher_id, SINGLETON_WALLET.launcher_puzzle_hash, DELAY_SECONDS, DELAY_PUZZLE_HASH
    )
    farmed_coin = coin_store.farm_coin(pool_reward_puzzle_hash, now)
    now.seconds += 500
    now.height = uint32(now.height + 1)

    p2_singleton_coins = filter_p2_singleton(PUZZLE_DB, [farmed_coin])
    assert p2_singleton_coins == [farmed_coin]

    assert len(list(coin_store.all_unspent_coins())) == 3

    #######
    # now collect the `p2_singleton` for the pool

    for coin in p2_singleton_coins:
        p2_singleton_coin_spend, singleton_conditions = claim_p2_singleton(PUZZLE_DB, SINGLETON_WALLET, coin)

        coin_spend = SINGLETON_WALLET.coin_spend_for_conditions(
            PUZZLE_DB,
            pool_leaving_spend_type="claim-p2-nft",
            pool_reward_amount=p2_singleton_coin_spend.coin.amount,
            pool_reward_height=now.height - 1,
        )
        spend_bundle = WalletSpendBundle([coin_spend, p2_singleton_coin_spend], G2Element())

        _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)
        now.seconds += 500
        now.height = uint32(now.height + 1)

        SINGLETON_WALLET.update_state(PUZZLE_DB, removals)

    assert len(list(coin_store.all_unspent_coins())) == 3

    #######
    # now finish leaving the pool

    initial_singleton_puzzle = adaptor_for_singleton_inner_puzzle(ANYONE_CAN_SPEND_PUZZLE)

    coin_spend = SINGLETON_WALLET.coin_spend_for_conditions(
        PUZZLE_DB,
        pool_leaving_spend_type="exit-waiting-room",
        key_value_list=[("foo1", "bar2"), ("foo2", "baz5")],
        destination_puzzle_hash=initial_singleton_puzzle.get_tree_hash(),
    )
    spend_bundle = WalletSpendBundle([coin_spend], G2Element())

    full_puzzle = singleton_puzzle(
        SINGLETON_WALLET.launcher_id, SINGLETON_WALLET.launcher_puzzle_hash, initial_singleton_puzzle
    )

    PUZZLE_DB.add_puzzle(full_puzzle)

    try:
        _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)
        assert 0
    except BadSpendBundleError as ex:
        assert ex.args[0] == "condition validation failure Err.ASSERT_HEIGHT_RELATIVE_FAILED"

    now.seconds += 350000
    now.height = uint32(now.height + 1445)

    _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)

    SINGLETON_WALLET.update_state(PUZZLE_DB, removals)

    assert len(list(coin_store.all_unspent_coins())) == 3

    #######
    # now spend to oblivion with the `-113` hack

    coin_spend = SINGLETON_WALLET.coin_spend_for_conditions(
        PUZZLE_DB, conditions=[Program.to([ConditionOpcode.CREATE_COIN, 0, -113])]
    )
    spend_bundle = WalletSpendBundle([coin_spend], G2Element())
    debug_spend_bundle(spend_bundle)

    _, removals = coin_store.update_coin_store_for_spend_bundle(spend_bundle, now, MAX_BLOCK_COST_CLVM)
    update_count = SINGLETON_WALLET.update_state(PUZZLE_DB, removals)

    assert update_count == 0

    assert len(list(coin_store.all_unspent_coins())) == 2
