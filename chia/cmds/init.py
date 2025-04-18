from __future__ import annotations

import click

from chia.cmds.cmd_classes import ChiaCliContext


@click.command("init", help="Create or migrate the configuration")
@click.option(
    "--create-certs",
    "-c",
    default=None,
    help="Create new SSL certificates based on CA in [directory]",
    type=click.Path(),
)
@click.option(
    "--fix-ssl-permissions",
    is_flag=True,
    help="Attempt to fix SSL certificate/key file permissions",
)
@click.option("--testnet", is_flag=True, help="Configure this chia install to connect to the testnet")
@click.option("--set-passphrase", "-s", is_flag=True, help="Protect your keyring with a passphrase")
@click.option(
    "--v1-db",
    is_flag=True,
    help="Initialize the blockchain database in v1 format (compatible with older versions of the full node)",
)
@click.pass_context
def init_cmd(
    ctx: click.Context,
    create_certs: str,
    fix_ssl_permissions: bool,
    testnet: bool,
    set_passphrase: bool,
    v1_db: bool,
) -> None:
    """
    Create a new configuration or migrate from previous versions to current

    \b
    Follow these steps to create new certificates for a remote harvester:
    - Make a copy of your Farming Machine CA directory: ~/.chia/[version]/config/ssl/ca
    - Shut down all chia daemon processes with `chia stop all -d`
    - Run `chia init -c [directory]` on your remote harvester,
      where [directory] is the copy of your Farming Machine CA directory
    - Get more details on remote harvester on Chia wiki:
      https://github.com/Chia-Network/chia-blockchain/wiki/Farming-on-many-machines
    """
    from pathlib import Path

    from chia.cmds.init_funcs import init
    from chia.cmds.passphrase_funcs import initialize_passphrase

    if set_passphrase:
        initialize_passphrase()

    init(
        Path(create_certs) if create_certs is not None else None,
        ChiaCliContext.set_default(ctx).root_path,
        fix_ssl_permissions,
        testnet,
        v1_db,
    )
