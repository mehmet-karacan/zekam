"""`zekam policy`, `zekam secret` ve `zekam auth` komutlari.

Hicbir komut secret **degeri** yazdirmaz veya kabul etmez. Deger yalnizca
yapilandirilmis arka uctan (ortam degiskeni, keychain, Vault, KMS) cozulur.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.governance import GovernanceService, default_capabilities
from zekam.application.realm_context import RealmContext
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.security import SecretBackend, SecretRef, SecretStatus
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail, fail_from

policy_app = typer.Typer(name="policy", help="Policy ve capability islemleri", no_args_is_help=True)
secret_app = typer.Typer(
    name="secret", help="SecretRef metadata islemleri (deger tutulmaz)", no_args_is_help=True
)
auth_app = typer.Typer(name="auth", help="Exact authorization ledgeri", no_args_is_help=True)

console = Console()


def _service(realm_context: RealmContext) -> GovernanceService:
    return GovernanceService(realm_context.connection, realm_context.realm)


# -- policy -------------------------------------------------------------------------


@policy_app.command("init")
def policy_init_command(
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten olusturur")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Varsayilan policy ve temel yetenekleri olusturur (idempotent)."""
    if not apply:
        console.print("olusturulacak: varsayilan policy + 7 temel capability")
        console.print("[yellow]Dry-run. Olusturmak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm, create_realm=True) as realm_context:
            service = _service(realm_context)
            document = service.ensure_default_policy()
            created = 0
            for capability in default_capabilities(realm_context.realm_id):
                if service.capabilities.current(capability.name) is None:
                    service.capabilities.append(capability)
                    created += 1
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(
        f"[green]Hazir:[/green] policy {document.name} r{document.revision}, "
        f"{created} yeni capability"
    )


@policy_app.command("show")
def policy_show_command(
    name: Annotated[str, typer.Option("--ad", help="Policy adi")] = "varsayilan",
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Aktif policy belgesini yazar."""
    try:
        with RealmSession(home, realm) as realm_context:
            document = _service(realm_context).active_policy(name)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document.as_dict(), ensure_ascii=False, default=str))


@policy_app.command("capabilities")
def capability_list_command(
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kayitli yetenekleri listeler. Yetenek beyani yetki degildir."""
    try:
        with RealmSession(home, realm) as realm_context:
            capabilities = _service(realm_context).capabilities.list_all()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    table = Table(title="Capability kayitlari (yetki degil)")
    table.add_column("Ad")
    table.add_column("Tur")
    table.add_column("Surum")
    table.add_column("Aciklama")
    for capability in capabilities:
        table.add_row(
            capability.name, capability.kind.value, str(capability.revision), capability.description
        )
    console.print(table)


# -- secret -------------------------------------------------------------------------


@secret_app.command("add")
def secret_add_command(
    name: Annotated[str, typer.Argument(help="SecretRef adi")],
    provider: Annotated[str, typer.Option("--provider", help="Saglayici kimligi")],
    purpose: Annotated[str, typer.Option("--amac", help="Kullanim amaci")],
    locator: Annotated[
        str, typer.Option("--locator", help="Arka uctaki mantiksal ad (deger DEGIL)")
    ],
    operation: Annotated[
        list[str], typer.Option("--operasyon", help="Izinli operasyon (tekrarlanabilir)")
    ],
    backend: Annotated[
        SecretBackend, typer.Option("--backend", help="Deger arka ucu")
    ] = SecretBackend.ENVIRONMENT,
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten kaydeder")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """SecretRef metadata kaydi olusturur. Deger asla parametre olarak alinmaz."""
    if "=" in locator and len(locator.split("=", 1)[1]) > 0:
        raise fail(
            "Locator bir deger gibi gorunuyor. Yalnizca arka uctaki adi verin.",
        )
    if not apply:
        console.print(f"kaydedilecek: {name} -> {backend.value}:{locator}")
        console.print("[yellow]Dry-run. Kaydetmek icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            reference = SecretRef.create(
                realm_id=realm_context.realm_id,
                name=name,
                provider=provider,
                purpose=purpose,
                allowed_operations=tuple(operation),
                store_backend=backend,
                store_locator=locator,
            )
            service.secrets.add(reference)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Kaydedildi:[/green] {reference.name} v{reference.version}")


@secret_app.command("list")
def secret_list_command(
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """SecretRef kayitlarini listeler. Deger gosterilmez."""
    try:
        with RealmSession(home, realm) as realm_context:
            references = _service(realm_context).secrets.list_all()
            rows = [reference.as_dict() for reference in references]
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False, default=str))
        return
    table = Table(title="SecretRef kayitlari (deger tutulmaz)")
    table.add_column("Ad")
    table.add_column("Saglayici")
    table.add_column("Amac")
    table.add_column("Durum")
    table.add_column("Arka uc")
    for row in rows:
        table.add_row(
            str(row["name"]),
            str(row["provider"]),
            str(row["purpose"]),
            str(row["status"]),
            str(row["store_backend"]),
        )
    console.print(table)


@secret_app.command("revoke")
def secret_revoke_command(
    name: Annotated[str, typer.Argument(help="SecretRef adi")],
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten uygular")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """SecretRef'i iptal eder; sonraki cozumlemeler reddedilir."""
    if not apply:
        console.print(f"iptal edilecek: {name}")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            reference = service.secrets.current_by_name(name)
            if reference is None:
                raise fail(f"SecretRef bulunamadi: {name}", 4)
            service.secrets.set_status(reference.id, SecretStatus.REVOKED)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print(f"[green]Iptal edildi:[/green] {name}")


# -- authorization ---------------------------------------------------------------------


@auth_app.command("list")
def auth_list_command(
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Gecerli yetkileri listeler."""
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            service.authorizations.expire_stale()
            rows = [item.as_dict() for item in service.authorizations.list_active()]
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False, default=str))
        return
    table = Table(title="Aktif yetkiler")
    table.add_column("Kimlik")
    table.add_column("Risk")
    table.add_column("Etkiler")
    table.add_column("Bitis")
    for row in rows:
        scope = row["scope"]
        table.add_row(
            str(row["id"]),
            str(row["risk"]),
            ", ".join(scope["allowed_effects"]),
            str(row["expires_at"]),
        )
    console.print(table)


@auth_app.command("show")
def auth_show_command(
    authorization_id: Annotated[str, typer.Argument(help="Yetki kimligi")],
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yetkiyi ve denetim izini yazar."""
    try:
        identifier = UUID(authorization_id)
    except ValueError as exc:
        raise fail("Gecersiz yetki kimligi", 4) from exc
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            authorization = service.authorizations.get(identifier)
            document = authorization.as_dict()
            document["audit"] = list(service.audit.for_subject("authorization", str(identifier)))
            document["valid_now"] = authorization.is_valid_at(dt.datetime.now(dt.UTC))
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@auth_app.command("revoke")
def auth_revoke_command(
    authorization_id: Annotated[str, typer.Argument(help="Yetki kimligi")],
    reason: Annotated[str, typer.Option("--gerekce", help="Iptal gerekcesi")],
    apply: Annotated[bool, typer.Option("--uygula", help="Gercekten uygular")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yetkiyi iptal eder."""
    try:
        identifier = UUID(authorization_id)
    except ValueError as exc:
        raise fail("Gecersiz yetki kimligi", 4) from exc
    if not apply:
        console.print(f"iptal edilecek: {identifier}")
        console.print("[yellow]Dry-run. Uygulamak icin --uygula verin.[/yellow]")
        return
    try:
        with RealmSession(home, realm) as realm_context:
            revoked = _service(realm_context).revoke_authorization(identifier, reason)
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if not revoked:
        raise fail("Yetki iptal edilemedi; zaten terminal durumda olabilir", 6)
    console.print(f"[green]Iptal edildi:[/green] {identifier}")


@auth_app.command("audit")
def auth_audit_command(
    limit: Annotated[int, typer.Option("--adet", help="Kayit sayisi")] = 20,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Son denetim kayitlarini yazar."""
    try:
        with RealmSession(home, realm) as realm_context:
            rows = list(_service(realm_context).audit.recent(limit=limit))
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(rows, ensure_ascii=False, default=str))
