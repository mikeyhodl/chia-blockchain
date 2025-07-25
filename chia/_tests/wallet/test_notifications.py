from __future__ import annotations

import random
import tempfile
from pathlib import Path
from typing import Any, Optional

import pytest
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64

from chia._tests.util.time_out_assert import time_out_assert, time_out_assert_not_none
from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.simulator.full_node_simulator import FullNodeSimulator
from chia.simulator.simulator_protocol import FarmNewBlockProtocol
from chia.types.peer_info import PeerInfo
from chia.util.db_wrapper import DBWrapper2
from chia.wallet.notification_store import NotificationStore
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG


# For testing backwards compatibility with a DB change to add height
@pytest.mark.anyio
async def test_notification_store_backwards_compat() -> None:
    # First create the DB the way it would have otheriwse been created
    with tempfile.TemporaryDirectory() as temporary_directory:
        db_name = Path(temporary_directory).joinpath("test.sqlite")
        db_name.parent.mkdir(parents=True, exist_ok=True)
        async with DBWrapper2.managed(
            database=db_name,
        ) as db_wrapper:
            async with db_wrapper.writer_maybe_transaction() as conn:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS notifications(coin_id blob PRIMARY KEY,msg blob,amount blob)"
                )
                cursor = await conn.execute(
                    "INSERT OR REPLACE INTO notifications (coin_id, msg, amount) VALUES(?, ?, ?)",
                    (
                        bytes32.zeros,
                        bytes([0] * 10),
                        bytes([0]),
                    ),
                )
                await cursor.close()

            await NotificationStore.create(db_wrapper)
            await NotificationStore.create(db_wrapper)


@pytest.mark.parametrize(
    "trusted",
    [True, False],
)
@pytest.mark.anyio
async def test_notifications(
    self_hostname: str, two_wallet_nodes: Any, trusted: Any, seeded_random: random.Random
) -> None:
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api: FullNodeSimulator = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_1, server_0 = wallets[0]
    wallet_node_2, server_1 = wallets[1]
    wsm_1 = wallet_node_1.wallet_state_manager
    wsm_2 = wallet_node_2.wallet_state_manager
    wallet_1 = wsm_1.main_wallet
    wallet_2 = wsm_2.main_wallet

    async with wallet_1.wallet_state_manager.new_action_scope(DEFAULT_TX_CONFIG, push=True) as action_scope:
        ph_1 = await action_scope.get_puzzle_hash(wallet_1.wallet_state_manager)
    async with wallet_2.wallet_state_manager.new_action_scope(DEFAULT_TX_CONFIG, push=True) as action_scope:
        ph_2 = await action_scope.get_puzzle_hash(wallet_2.wallet_state_manager)
    ph_token = bytes32.random(seeded_random)

    if trusted:
        wallet_node_1.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
        wallet_node_2.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_1.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}

    await server_0.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    await server_1.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)

    for i in range(2):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_1))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))
    await full_node_api.wait_for_wallets_synced(wallet_nodes=[wallet_node_1, wallet_node_2], timeout=30)

    funds_1 = sum(calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, 3))
    funds_2 = 0

    await time_out_assert(30, wallet_1.get_unconfirmed_balance, funds_1)
    await time_out_assert(30, wallet_1.get_confirmed_balance, funds_1)

    notification_manager_1 = wsm_1.notification_manager
    notification_manager_2 = wsm_2.notification_manager

    func = notification_manager_2.potentially_add_new_notification
    notification_manager_2.most_recent_args = tuple()

    async def track_coin_state(*args: Any) -> bool:
        notification_manager_2.most_recent_args = args
        result: bool = await func(*args)
        return result

    notification_manager_2.potentially_add_new_notification = track_coin_state

    allow_larger_height: Optional[int] = None
    allow_height: Optional[int] = None

    for case in ("block all", "block too low", "allow", "allow_larger", "block_too_large"):
        msg: bytes = bytes(case, "utf8")
        if case == "block all":
            wallet_node_2.config["enable_notifications"] = False
            wallet_node_2.config["required_notification_amount"] = 100
            AMOUNT = uint64(100)
            FEE = uint64(0)
        elif case == "block too low":
            wallet_node_2.config["enable_notifications"] = True
            AMOUNT = uint64(1)
            FEE = uint64(0)
        elif case in {"allow", "allow_larger"}:
            wallet_node_2.config["required_notification_amount"] = 750000000000
            if case == "allow_larger":
                AMOUNT = uint64(1000000000000)
            else:
                AMOUNT = uint64(750000000000)
            FEE = uint64(1)
        elif case == "block_too_large":
            msg = bytes([0] * 10001)
            AMOUNT = uint64(750000000000)
            FEE = uint64(0)
        else:
            raise Exception(f"Unhandled case: {case!r}")
        peak = full_node_api.full_node.blockchain.get_peak()
        assert peak is not None
        if case == "allow":
            allow_height = peak.height + 1
        if case == "allow_larger":
            allow_larger_height = peak.height + 1
        async with notification_manager_1.wallet_state_manager.new_action_scope(
            DEFAULT_TX_CONFIG, push=True
        ) as action_scope:
            await notification_manager_1.send_new_notification(ph_2, msg, AMOUNT, action_scope, fee=FEE)
        [tx] = action_scope.side_effects.transactions
        await time_out_assert_not_none(
            5,
            full_node_api.full_node.mempool_manager.get_spendbundle,
            tx.spend_bundle.name(),
        )
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph_token))

        funds_1 = funds_1 - AMOUNT - FEE
        funds_2 += AMOUNT

        await time_out_assert(30, wallet_1.get_unconfirmed_balance, funds_1)
        await time_out_assert(30, wallet_1.get_confirmed_balance, funds_1)
        await time_out_assert(30, wallet_2.get_unconfirmed_balance, funds_2)
        await time_out_assert(30, wallet_2.get_confirmed_balance, funds_2)

    assert allow_larger_height is not None
    assert allow_height is not None

    notifications = await notification_manager_2.notification_store.get_all_notifications(pagination=(0, 2))
    assert len(notifications) == 2
    assert notifications[0].message == b"allow_larger"
    assert notifications[0].height == allow_larger_height
    notifications = await notification_manager_2.notification_store.get_all_notifications(pagination=(1, None))
    assert len(notifications) == 1
    assert notifications[0].message == b"allow"
    assert notifications[0].height == allow_height
    notifications = await notification_manager_2.notification_store.get_all_notifications(pagination=(0, 1))
    assert len(notifications) == 1
    assert notifications[0].message == b"allow_larger"
    notifications = await notification_manager_2.notification_store.get_all_notifications(pagination=(None, 1))
    assert len(notifications) == 1
    assert notifications[0].message == b"allow_larger"
    assert (
        await notification_manager_2.notification_store.get_notifications([n.id for n in notifications])
        == notifications
    )

    sent_notifications = await notification_manager_1.notification_store.get_all_notifications()
    assert len(sent_notifications) == 0

    await notification_manager_2.notification_store.delete_all_notifications()
    assert len(await notification_manager_2.notification_store.get_all_notifications()) == 0
    await notification_manager_2.notification_store.add_notification(notifications[0])
    await notification_manager_2.notification_store.delete_notifications([n.id for n in notifications])
    assert len(await notification_manager_2.notification_store.get_all_notifications()) == 0

    assert not await func(*notification_manager_2.most_recent_args)
    await notification_manager_2.notification_store.delete_all_notifications()
    assert not await func(*notification_manager_2.most_recent_args)
