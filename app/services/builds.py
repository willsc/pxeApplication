from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, AssetStatus, BuildRecord, BuildState, Host, utcnow
from app.services.ipxe import token_prefix


def ensure_asset_for_host(db: Session, host: Host) -> Asset:
    if host.asset:
        return host.asset
    asset = Asset(
        host=host,
        asset_tag=host.hostname or host.mac,
        status=AssetStatus.ACTIVE,
        metadata_json={"created_from": "provisioning_callback"},
    )
    db.add(asset)
    db.flush()
    return asset


def start_build_record(db: Session, host: Host, token: str) -> BuildRecord:
    asset = ensure_asset_for_host(db, host) if host.asset else None
    image = host.profile.image if host.profile else None
    record = BuildRecord(
        host=host,
        asset=asset,
        profile_id=host.profile_id,
        image_id=image.id if image else None,
        state=BuildState.INSTALLING,
        token_prefix=token_prefix(token),
        metadata_json={
            "hostname": host.hostname,
            "mac": host.mac,
            "profile": host.profile.name if host.profile else None,
            "image": image.name if image else None,
            "os_type": image.os_type.value if image else None,
        },
    )
    db.add(record)
    db.flush()
    return record


def finish_latest_build_record(db: Session, host: Host, state: BuildState) -> BuildRecord:
    asset = ensure_asset_for_host(db, host)
    asset.status = AssetStatus.ACTIVE if state == BuildState.PROVISIONED else asset.status

    record = db.scalar(
        select(BuildRecord)
        .where(BuildRecord.host_id == host.id)
        .order_by(BuildRecord.started_at.desc(), BuildRecord.id.desc())
        .limit(1)
    )
    if not record:
        image = host.profile.image if host.profile else None
        record = BuildRecord(
            host=host,
            profile_id=host.profile_id,
            image_id=image.id if image else None,
            token_prefix=token_prefix(host.install_token),
            metadata_json={"created_from": "callback_without_config_event"},
        )
        db.add(record)
    record.asset = asset
    record.state = state
    record.completed_at = utcnow()
    db.flush()
    return record

