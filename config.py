import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            "Copy .env.example → .env and fill in your values."
        )
    return value


@dataclass
class Settings:
    ado_org_url: str = field(default_factory=lambda: _require("ADO_ORG_URL").rstrip("/"))
    ado_project: str = field(default_factory=lambda: _require("ADO_PROJECT"))
    ado_pat: str = field(default_factory=lambda: _require("ADO_PAT"))

    ado_team: str = field(
        default_factory=lambda: os.environ.get("ADO_TEAM", "").strip()
        or f"{os.environ.get('ADO_PROJECT', 'Unknown')} Team"
    )

    api_key: str = field(default_factory=lambda: os.environ.get("API_KEY", "").strip())
    server_name: str = field(
        default_factory=lambda: os.environ.get("MCP_SERVER_NAME", "Sprint Health").strip()
    )
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "8000")))

    at_risk_stale_days: int = field(
        default_factory=lambda: int(os.environ.get("AT_RISK_STALE_DAYS", "3"))
    )
    overload_multiplier: float = field(
        default_factory=lambda: float(os.environ.get("OVERLOAD_THRESHOLD_MULTIPLIER", "1.5"))
    )
    underload_multiplier: float = field(
        default_factory=lambda: float(os.environ.get("UNDERLOAD_THRESHOLD_MULTIPLIER", "0.5"))
    )


settings = Settings()
