from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORDER_SONG_", env_file=".env", extra="ignore")

    app_name: str = "order-song"
    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_exp_minutes: int = 60 * 24 * 14

    sqlite_path: str = "order_song.sqlite3"
    cors_allow_origins: str = "*"


settings = Settings()

