from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RECON_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://recon:recon@localhost:5432/recon"
    # 金额精度：交易/结算/记账币统一保留 2 位（演示用），币种精度表可覆盖
    ledger_decimal_places: int = 2
    # 差异容忍阈值（记账币绝对值，<= 阈值的金额差视为可自动匹配的舍入差）
    rounding_tolerance: str = "0.05"
    # 自动任务只能提示，不得自动核销未知差异（由工作流强制，此处仅做开关展示）
    auto_writeoff_allowed: bool = False


settings = Settings()
