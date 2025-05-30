from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

import pytest
from chia_rs import CoinSpend
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64
from clvm_tools import binutils

from chia._tests.util.db_connection import DBConnection
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.wallet.util.compute_additions import compute_additions
from chia.wallet.wallet_pool_store import WalletPoolStore


def make_child_solution(
    coin_spend: Optional[CoinSpend], new_coin: Optional[Coin], seeded_random: random.Random
) -> CoinSpend:
    new_puzzle_hash: bytes32 = bytes32.random(seeded_random)
    solution = "()"
    puzzle = f"(q . ((51 0x{new_puzzle_hash.hex()} 1)))"
    puzzle_prog = Program.to(binutils.assemble(puzzle))
    solution_prog = Program.to(binutils.assemble(solution))
    if new_coin is None:
        assert coin_spend is not None
        new_coin = compute_additions(coin_spend)[0]
    sol: CoinSpend = make_spend(
        new_coin,
        puzzle_prog,
        solution_prog,
    )
    return sol


async def assert_db_spends(store: WalletPoolStore, wallet_id: int, spends: list[CoinSpend]) -> None:
    db_spends = await store.get_spends_for_wallet(wallet_id)
    assert len(db_spends) == len(spends)
    for spend, (_, db_spend) in zip(spends, db_spends):
        assert spend == db_spend


@dataclass
class DummySpends:
    seeded_random: random.Random
    spends_per_wallet: dict[int, list[CoinSpend]] = field(default_factory=dict)

    def generate(self, wallet_id: int, count: int) -> None:
        current = self.spends_per_wallet.setdefault(wallet_id, [])
        for _ in range(count):
            coin = None
            last_spend = None if len(current) == 0 else current[-1]
            if last_spend is None:
                coin = Coin(bytes32.random(self.seeded_random), bytes32.random(self.seeded_random), uint64(12312))
            current.append(make_child_solution(coin_spend=last_spend, new_coin=coin, seeded_random=self.seeded_random))


class TestWalletPoolStore:
    @pytest.mark.anyio
    async def test_store(self, seeded_random: random.Random):
        async with DBConnection(1) as db_wrapper:
            store = await WalletPoolStore.create(db_wrapper)

            try:
                async with db_wrapper.writer():
                    coin_0 = Coin(bytes32.random(seeded_random), bytes32.random(seeded_random), uint64(12312))
                    coin_0_alt = Coin(bytes32.random(seeded_random), bytes32.random(seeded_random), uint64(12312))
                    solution_0: CoinSpend = make_child_solution(
                        coin_spend=None, new_coin=coin_0, seeded_random=seeded_random
                    )
                    solution_0_alt: CoinSpend = make_child_solution(
                        coin_spend=None, new_coin=coin_0_alt, seeded_random=seeded_random
                    )
                    solution_1: CoinSpend = make_child_solution(
                        coin_spend=solution_0, new_coin=None, seeded_random=seeded_random
                    )

                    assert await store.get_spends_for_wallet(0) == []
                    assert await store.get_spends_for_wallet(1) == []

                    await store.add_spend(1, solution_1, 100)
                    assert await store.get_spends_for_wallet(1) == [(100, solution_1)]

                    # Idempotent
                    await store.add_spend(1, solution_1, 100)
                    assert await store.get_spends_for_wallet(1) == [(100, solution_1)]

                    with pytest.raises(ValueError):
                        await store.add_spend(1, solution_1, 101)

                    # Rebuild cache, no longer present
                    raise RuntimeError("abandon transaction")
            except Exception:
                pass

            assert await store.get_spends_for_wallet(1) == []

            await store.add_spend(1, solution_1, 100)
            assert await store.get_spends_for_wallet(1) == [(100, solution_1)]

            solution_1_alt: CoinSpend = make_child_solution(solution_0_alt, new_coin=None, seeded_random=seeded_random)

            with pytest.raises(ValueError):
                await store.add_spend(1, solution_1_alt, 100)

            assert await store.get_spends_for_wallet(1) == [(100, solution_1)]

            solution_2: CoinSpend = make_child_solution(solution_1, new_coin=None, seeded_random=seeded_random)
            await store.add_spend(1, solution_2, 100)
            solution_3: CoinSpend = make_child_solution(solution_2, new_coin=None, seeded_random=seeded_random)
            await store.add_spend(1, solution_3, 100)
            solution_4: CoinSpend = make_child_solution(solution_3, new_coin=None, seeded_random=seeded_random)

            with pytest.raises(ValueError):
                await store.add_spend(1, solution_4, 99)

            await store.add_spend(1, solution_4, 101)
            await store.rollback(101, 1)
            assert await store.get_spends_for_wallet(1) == [
                (100, solution_1),
                (100, solution_2),
                (100, solution_3),
                (101, solution_4),
            ]
            await store.rollback(100, 1)
            assert await store.get_spends_for_wallet(1) == [
                (100, solution_1),
                (100, solution_2),
                (100, solution_3),
            ]
            with pytest.raises(ValueError):
                await store.add_spend(1, solution_1, 105)

            await store.add_spend(1, solution_4, 105)
            solution_5: CoinSpend = make_child_solution(solution_4, new_coin=None, seeded_random=seeded_random)
            await store.add_spend(1, solution_5, 105)
            await store.rollback(99, 1)
            assert await store.get_spends_for_wallet(1) == []


@pytest.mark.anyio
async def test_delete_wallet(seeded_random: random.Random) -> None:
    dummy_spends = DummySpends(seeded_random=seeded_random)
    for i in range(5):
        dummy_spends.generate(i, i * 5)
    async with DBConnection(1) as db_wrapper:
        store = await WalletPoolStore.create(db_wrapper)
        # Add the spends per wallet and verify them
        for wallet_id, spends in dummy_spends.spends_per_wallet.items():
            for i, spend in enumerate(spends):
                await store.add_spend(wallet_id, spend, uint32(i + wallet_id))
            await assert_db_spends(store, wallet_id, spends)
        # Remove one wallet after the other and verify before and after each
        for wallet_id, spends in dummy_spends.spends_per_wallet.items():
            # Assert the existence again here to make sure the previous removals did not affect other wallet_ids
            await assert_db_spends(store, wallet_id, spends)
            await store.delete_wallet(wallet_id)
            await assert_db_spends(store, wallet_id, [])
