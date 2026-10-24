"""Application settings, loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
APP_VERSION = "3.3.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Local server ---
    host: str = "127.0.0.1"
    port: int = 8787
    # Optional passcode protecting the UI. Empty = no passcode (local-only use).
    # Strongly recommended: the admin pages can reveal API keys and edit the DB.
    ui_passcode: str = ""
    # Other names the UI is opened with, comma separated (against DNS
    # rebinding); the loopback names always work. Empty with HOST=127.0.0.1 =
    # just those; empty with any other HOST = any name. "*" = any. Behind a
    # proxy: the public name.
    allowed_hosts: str = ""
    # Start even if another copy of the app seems to be using this database
    # (see services/instance.py). Only for when you know it is gone.
    instance_takeover: bool = False

    # --- Storage ---
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'scheduler.db'}"
    # The shared library: every video lives here once, in the folder of its
    # series, whatever number of queues publish it. It is also the watch
    # folder: a file copied in by hand is picked up on the next tick.
    media_library_dir: Path = BASE_DIR / "media" / "library"
    # Where the library was when this app was TikTok Scheduler: the database
    # keeps those absolute paths. Read as the same place under
    # media_library_dir, so they work without the old folder's link.
    legacy_library_dirs: list[Path] = [
        Path.home() / "Projects" / "Experiments" / "TikTokScheduler" / "media" / "library",
    ]
    # Where videos lived before the library (one copy per queue). Nothing is
    # written there any more; tools/library_copy.py and library_finalize.py
    # empty them into the library, and the admin page still reports them.
    media_inbox_dir: Path = BASE_DIR / "media" / "inbox"
    media_posted_dir: Path = BASE_DIR / "media" / "posted"
    media_failed_dir: Path = BASE_DIR / "media" / "failed"
    secrets_dir: Path = BASE_DIR / ".secrets"
    backups_dir: Path = BASE_DIR / "data" / "backups"
    # A copy of the database every this many hours, on its own (0 = never),
    # and how many of those automatic copies are kept. Copies made by hand
    # are never deleted. ~10 MB each for a year of history.
    backup_auto_hours: int = 24
    backup_keep: int = 14
    # Base64 Fernet key. If empty, one is generated into .secrets/encryption.key
    app_encryption_key: str = ""

    # --- Scheduler ---
    scheduler_tick_seconds: int = 20
    default_timezone: str = "Europe/Madrid"
    max_publish_attempts: int = 3
    # Seconds between status polls of a post Zernio has accepted.
    status_poll_seconds: int = 30
    # Give up waiting for a final status after this long.
    status_timeout_minutes: int = 60
    # Pick up the files copied into media/library by hand.
    watch_folder_enabled: bool = True
    # Published and failed videos listed per account in the UI (the latest
    # ones). Everything still on its way is always listed, whatever its count.
    state_history_limit: int = 300

    # --- Notifications (optional; the value saved in Ajustes wins) ---
    webhook_url: str = ""

    # --- Analytics ---
    # How often the per-post numbers are refreshed from Zernio. TikTok updates
    # them slowly (profile views and completion rate land 24-48 h late), so
    # anything under an hour just burns requests.
    analytics_refresh_minutes: int = 180
    # How far back each sync looks. Zernio's own ceiling is 366 days.
    analytics_window_days: int = 90

    # --- Comments ---
    comments_enabled: bool = True
    # Zernio caches the comment list for ten minutes; polling faster than that
    # returns the same page.
    comments_poll_minutes: int = 30
    # A video stops being polled once it is this old: comments dry up and the
    # request budget is better spent on the recent ones.
    comments_window_days: int = 14
    # Videos refreshed per account per pass.
    comments_batch: int = 5

    # --- Automatic captions and hashtags ---
    # When a video enters a queue with no text of its own, it is transcribed
    # locally with Whisper and a caption plus hashtags are written from that.
    caption_ai_enabled: bool = True
    # tiny | base | small | medium | large-v3. "small" is the sweet spot on a
    # laptop: ~500 MB, a few seconds per minute of speech, good Spanish.
    whisper_model: str = "small"
    # ISO code, or empty to let Whisper detect the language of each video.
    whisper_language: str = "es"
    # auto | cpu | cuda, and the CTranslate2 compute type. int8 on CPU is the
    # fast, small default; float32 is more precise and twice as slow.
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    # Where the Whisper weights are downloaded the first time (~500 MB).
    whisper_models_dir: Path = BASE_DIR / "data" / "models" / "whisper"
    # A local LLM (Ollama) turns the transcript (or the video's .txt sheet)
    # into a real description and suggests on-topic hashtags. Optional:
    # without it, the caption is the video's opening line and the hashtags
    # come from keywords. Empty = off.
    ollama_url: str = "http://127.0.0.1:11434"
    # Empty = the smallest installed model of 4 GB or more, a Gemma 4 (or
    # Gemma 3) first; the largest one if they are all smaller.
    ollama_model: str = ""
    ollama_timeout_seconds: int = 120
    # How long Ollama keeps the model loaded after a text. Short on purpose: a
    # queue being written keeps it warm (texts go one after another), and it
    # leaves memory soon after, instead of competing with whatever runs next.
    ollama_keep_alive: str = "2m"
    # A second pass that sends the model its own draft with the problems the
    # checks found (other alphabets, English, stock openers, repeats…).
    caption_editor: bool = True
    # Seconds of audio Whisper listens to when a .txt sheet comes with the
    # video, to confirm the sheet is about this video. 0 = trust the name.
    caption_sheet_check_seconds: int = 20
    # Hashtags per video, counting the fixed ones from Ajustes. TikTok counts
    # five at most (since August 2025), so anything above 5 is capped.
    hashtag_max: int = 5
    # Broad reach tags (#parati, #fyp…) added at the end when there is room.
    # 0 by default: they name no topic and take a slot a topic tag uses better.
    hashtag_reach: int = 0
    # The topic bank (categories, keywords, hashtags) the selector draws from.
    hashtag_bank_file: Path = BASE_DIR / "data" / "hashtags.json"
    # A video whose text is still being written is held back from publishing
    # for at most this long; then it goes out with whatever it has.
    caption_max_wait_minutes: int = 10

    # --- Zernio ---
    # Overridable so the test-suite and tools/demo.sh can point it at the mock.
    zernio_api_base: str = "https://zernio.com/api/v1"
    # A 4 GB upload can legitimately take a while.
    zernio_timeout_seconds: int = 600
    # After Zernio reports success, keep polling this long for the TikTok URL
    # (TikTok exposes the video id a few minutes after publishing).
    zernio_url_grace_minutes: int = 5

    # --- Delegation ("mandar la cola a Zernio de golpe") ---
    # Videos uploaded per tick when a whole queue is being handed over. One at
    # a time keeps a big batch from hogging the connection and the request
    # budget, and keeps the queue view moving while it happens.
    delegate_batch_per_tick: int = 1
    # How often a video already parked in Zernio is asked about, once its hour
    # has passed. Minutes, not seconds: there can be dozens waiting.
    delegated_poll_minutes: int = 10
    # Zernio holds over-cap posts and releases them when the rolling 24 h
    # window frees a slot, so a delegated video may legitimately publish hours
    # late. Past this, it is reported as failed.
    delegated_timeout_hours: int = 48

    @property
    def legacy_media_dirs(self) -> tuple[Path, Path, Path]:
        """The pre-library folders, in the order the tools walk them."""
        return (Path(self.media_inbox_dir), Path(self.media_posted_dir),
                Path(self.media_failed_dir))

    @property
    def media_dirs(self) -> tuple[Path, ...]:
        """Every folder a video of this app may be served from or deleted in."""
        return (Path(self.media_library_dir), *self.legacy_media_dirs)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def database_path(self) -> Path | None:
        """Filesystem path of the SQLite file, or None for other databases."""
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url[len(prefix):])


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.media_library_dir, s.secrets_dir, s.backups_dir):
        Path(d).mkdir(parents=True, exist_ok=True)
    Path(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
    return s
