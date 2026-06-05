"""Application configuration via pydantic-settings.

Supports layered precedence: CLI flag > env var (``ANAPLAN_AUDIT_`` prefix) >
``.env`` > ``settings.json`` > defaults.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkspaceModelCombo(BaseModel):
    """A workspace/model pair for filtering."""

    model_config = ConfigDict(populate_by_name=True)

    workspaceId: str
    modelId: str


class AnaplanUris(BaseModel):
    """Base URLs for each Anaplan API surface."""

    model_config = ConfigDict(populate_by_name=True)

    authServiceUri: str = "https://us1a.app.anaplan.com/token/authenticate"
    authTokenVerify: str = "https://us1a.app.anaplan.com/token/validate"
    oauthServiceUri: str = "https://us1a.app.anaplan.com/oauth"
    integrationUri: str = "https://api.anaplan.com/2/0"
    auditUri: str = "https://audit.anaplan.com/audit/api/1"
    scimUri: str = "https://scim.anaplan.com"
    cloudWorksUri: str = "https://api.cloudworks.anaplan.com/2/0"


class TargetModelObjects(BaseModel):
    """Object IDs within the target Audit Reporting Model."""

    model_config = ConfigDict(populate_by_name=True)

    auditFileId: str = ""
    auditImportId: str = ""
    lastRunFileId: str = ""
    lastRunImportId: str = ""


class TargetModelConfig(BaseModel):
    """Target Anaplan model for the upload step."""

    model_config = ConfigDict(populate_by_name=True)

    workspaceId: str = ""
    modelId: str = ""
    objects: TargetModelObjects = TargetModelObjects()


class ModelHistoryConfig(BaseModel):
    """Configuration for the Anaplan Model History feature."""

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    exportActionName: str = "MODEL_HISTORY_EXPORT"
    exportTimeoutSeconds: int = 600
    retentionYears: int = 2
    anaplanProcess: str = "Load Model History"

    # --- Concurrency ---
    maxConcurrentExports: int = 5
    """Maximum parallel model history exports.

    Each worker fires the export task, polls for completion, and downloads
    the result independently.  SQLite writes are always serialised on the
    main thread after all exports finish.  Raise this value for tenants with
    many models; lower it if you hit API rate limits.
    """

    # --- Backup ---
    backupBeforePurge: bool = True
    """Create a timestamped backup of the SQLite database before each purge.

    Backups are written alongside the database file with the suffix
    ``_backup_YYYYMMDD_HHMMSS.db``.  Old backups beyond *maxBackupsToKeep*
    are removed automatically.
    """

    maxBackupsToKeep: int = 7
    """Number of most-recent backups to retain when *backupBeforePurge* is true."""


class Settings(BaseSettings):
    """Top-level application settings.

    Field names match the v1 ``settings.json`` keys exactly so that existing
    customer configuration files work without modification.
    """

    model_config = SettingsConfigDict(
        env_prefix="ANAPLAN_AUDIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Feature flags ---
    auditEnabled: bool = True
    """Run the full audit extract-transform-load pipeline (Steps 1-6).

    Set to ``false`` to skip the audit entirely and run only Model History.
    At least one of ``auditEnabled`` or ``modelHistory.enabled`` must be
    ``true``; the validator below will raise a :class:`ConfigError` otherwise.
    """

    # --- Core ---
    authenticationMode: Literal["basic", "cert_auth", "OAuth"] = "OAuth"
    anaplanTenantName: str = ""
    database: str = "anaplan_audit.db"
    lastRun: int = 0
    auditBatchSize: int = 1000
    workspaceModelFilterApproach: Literal["select", "skip"] = "select"
    workspaceModelCombos: list[WorkspaceModelCombo] = []

    # --- URIs ---
    uris: AnaplanUris = AnaplanUris()

    # --- Target model ---
    targetAnaplanModel: TargetModelConfig = TargetModelConfig()

    # --- Model History ---
    modelHistory: ModelHistoryConfig = ModelHistoryConfig()

    # --- Cert auth ---
    certPublicPath: str = ""
    certPrivatePath: str = ""

    # --- OAuth ---
    rotatableToken: bool = True

    # --- Basic auth (env-only, never in settings.json) ---
    basic_username: str = ""
    basic_password: str = ""

    @field_validator("lastRun")
    @classmethod
    def _warn_stale_last_run(cls, v: int) -> int:
        """Warn if lastRun is more than 30 days old."""
        import time

        if v > 0:
            age_days = (time.time() - v) / 86400
            if age_days > 30:
                warnings.warn(
                    f"lastRun is {age_days:.0f} days old; "
                    "Anaplan only retains 30 days of audit data.",
                    UserWarning,
                    stacklevel=2,
                )
        return v

    @model_validator(mode="after")
    def _validate_feature_flags(self) -> Settings:
        """Ensure at least one feature is enabled."""
        if not self.auditEnabled and not self.modelHistory.enabled:
            from anaplan_audit.exceptions import ConfigError

            raise ConfigError(
                "Both auditEnabled and modelHistory.enabled are false — "
                "nothing to do. Enable at least one feature.",
            )
        return self

    @model_validator(mode="after")
    def _validate_auth_requirements(self) -> Settings:
        """Validate auth-mode-specific requirements at startup."""
        if self.authenticationMode == "cert_auth":
            if self.certPublicPath:
                pub = Path(self.certPublicPath.split(":")[0])
                if not pub.exists():
                    from anaplan_audit.exceptions import ConfigError

                    raise ConfigError(
                        f"Certificate public key not found: {pub}",
                        context={"path": str(pub)},
                    )
            if self.certPrivatePath:
                priv = Path(self.certPrivatePath.split(":")[0])
                if not priv.exists():
                    from anaplan_audit.exceptions import ConfigError

                    raise ConfigError(
                        f"Certificate private key not found: {priv}",
                        context={"path": str(priv)},
                    )
        return self


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings with JSON file as a base layer.

    Args:
        config_path: Path to ``settings.json``.  Defaults to ``./settings.json``.

    Returns:
        A validated :class:`Settings` instance.
    """
    path = config_path or Path("settings.json")
    init_kwargs: dict[str, Any] = {}
    if path.exists():
        with open(path) as f:
            init_kwargs = json.load(f)
    return Settings(**init_kwargs)
