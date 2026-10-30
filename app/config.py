from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str
    secret_key: str = Field(min_length=32)
    media_root: Path = Path('/media')
    base_url: str = 'http://localhost:8000'
    max_file_bytes: int = Field(default=1_000_000_000, gt=0)
    max_total_bytes: int = Field(default=30_000_000_000, gt=0)
    retention_days: int = Field(default=60, ge=1, le=3650)
    upload_timeout: int = Field(default=300, ge=5, le=3600)
    job_interval: int = 60
    # 猫の声らしい区間の候補に使うYAMNet。ない場合は音量の候補だけを使う。
    sound_model_path: Path = Path('/opt/meme/yamnet.tflite')

settings = Settings()
