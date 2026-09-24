"""All configuration in one typed, environment-driven object (DESIGN.md §8).

`Settings` (a pydantic-settings model) reads each field from the environment (or
a `.env` file), coercing/validating types — so a bad `SMTP_PORT=abc` fails fast
at startup rather than deep in a request. The defaults are dev-friendly; prod
overrides via real env vars (12-factor style: config lives in the environment,
never in code). `get_settings()` is `lru_cache`d so the whole app shares one
instance and the env is read once.
"""
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# The dev default is public (it's in the repo), so anyone could mint tokens for any
# user against a deployment that kept it. `Settings` refuses it under ENV=prod.
DEV_JWT_SECRET = "dev-secret-change-me"
MIN_PROD_JWT_SECRET_LEN = 32
# How long one account-email arq job may run before arq kills it (worker.main sets it as the
# job's own timeout). Lives here, not in worker/main.py, so `Settings` can validate against it
# without importing the worker.
EMAIL_JOB_TIMEOUT_SECONDS = 60

# Public dev default for the TOTP encryption key (a Fernet key), for the same reason as
# above: it's in the repo, so `Settings` refuses it under ENV=prod.
DEV_TOTP_KEY = "0lJlIcfZk08KdII_ZHhWohsb9MXwwf7RXmtyu4g5pvo="


class Settings(BaseSettings):
    """Typed application config. Field name `foo_bar` ← env var `FOO_BAR`.

    `extra="ignore"` means unrelated env vars don't cause errors; `env_file=".env"`
    loads a local dotenv in dev (real env vars still win over it).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # A closed set, not a free string: `secure` cookies, the dev-only routes and the
    # seed-user guard all key off `env == "prod"`, so `ENV=production` (or `Prod`)
    # would silently ship non-Secure cookies. Failing at startup is the loud check.
    env: Literal["dev", "prod"] = "dev"
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/shikomi"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = DEV_JWT_SECRET
    # Comma-separated retired signing secrets that still *verify* (never sign), so
    # rotating `jwt_secret` doesn't log everyone out. Drop an entry once
    # `jwt_access_ttl_seconds` has passed since it stopped signing (DESIGN.md §3.6).
    jwt_previous_secrets: str = ""

    # Two-factor auth (DESIGN.md §4.1). TOTP secrets are stored *encrypted* (Fernet), so a
    # database leak alone can't bypass the second factor. `totp_encryption_key` encrypts
    # new secrets; `totp_previous_keys` (comma-separated) still decrypt, so the key can be
    # rotated without bricking every enrolled user. Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    totp_encryption_key: str = DEV_TOTP_KEY
    totp_previous_keys: str = ""
    totp_issuer: str = "shikomi"  # the label authenticator apps show next to the account
    mfa_token_ttl_seconds: int = 300  # how long a password-verified login may wait for its code
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 2_592_000

    judge_image: str = "shikomi-judge:latest"
    # Sandbox image for `language="js"` problems (DESIGN.md §13) — a separate
    # node:slim-based image (judge/Dockerfile.js), selected per submission by
    # worker/judging.py's `IMAGE_BY_LANGUAGE` instead of the single Python image.
    judge_image_js: str = "shikomi-judge-js:latest"
    # Sandbox image for `language="mysql"` problems (DESIGN.md §13,
    # docs/adr/0002-sql-judge-engine-mysql-vs-mariadb.md) — a MariaDB-based
    # image (judge/Dockerfile.sql-mysql) that boots an ephemeral database
    # server per submission, unlike the two stateless-interpreter images
    # above. Needs a larger `--tmpfs` than they do (worker/judging.py's
    # TMPFS_SIZE_MB_BY_LANGUAGE) to fit MariaDB's data directory.
    judge_image_sql: str = "shikomi-judge-sql:latest"
    judge_max_concurrency: int = 4
    # How the worker runs a judge sandbox: `docker` shells `docker run` against the
    # host daemon (compose / single VPS); `k8s` launches a per-submission Pod via
    # the Kubernetes API (in-cluster). See worker/runner.py.
    judge_runner: str = "docker"
    judge_namespace: str = "default"   # namespace judge pods are created in (k8s runner)
    # Optional: pin judge pods to an isolated runtime (e.g. "gvisor") in prod.
    # None locally (kind has no such RuntimeClass) — see worker/k8s_runner.py.
    judge_runtime_class: str | None = None

    # Comma-separated in the env; parsed to a list below.
    cors_origins: str = "http://localhost:5173"

    # Auth brute-force protection (DESIGN.md §4.1).
    auth_rate_limit_per_minute: int = 10   # per-IP request cap on auth endpoints
    # Trust `X-Forwarded-For` for the client IP (the rate-limit key). Enable ONLY
    # when the API sits strictly behind a proxy you control (Caddy) that sets it —
    # otherwise a direct client forges the header to dodge the per-IP cap. Defaults
    # off (fail closed: use the socket peer) so a misconfigured deploy is safe.
    trust_proxy: bool = False
    # Per-account login lockout: after N failed logins within the window, the
    # account is locked for the cooldown (resets on a successful login).
    login_max_failures: int = 5
    login_failure_window_seconds: int = 900   # 15 min
    login_lockout_seconds: int = 900          # 15 min

    # Breached-password screening (app/breached_passwords.py): new passwords are
    # checked against Have I Been Pwned's range API, sending only a 5-char SHA-1
    # prefix. On a timeout/outage the check fails open (local policy only), so the
    # timeout bounds how much an HIBP hiccup can slow a signup. Turn the check off
    # for air-gapped deploys with no outbound internet.
    breached_password_check: bool = True
    breached_password_timeout_seconds: float = 2.0
    breached_password_api_url: str = "https://api.pwnedpasswords.com/range"

    # Email (verification + password reset). `console` logs to stdout and an
    # in-memory outbox (dev/tests); `smtp` uses the SMTP_* settings.
    email_backend: Literal["console", "smtp"] = "console"  # closed set: `SMTP` must not silently mean console
    email_from: str = "no-reply@example.com"
    frontend_base_url: str = "http://localhost:5173"
    email_verify_ttl_seconds: int = 86_400   # 24h
    password_reset_ttl_seconds: int = 3_600  # 1h
    # Operator alert when account mail stops flowing (worker/watchdog.py): the oldest job
    # waiting on `arq:accounts` for over `accounts_queue_stale_seconds` means the accounts
    # worker is down or wedged (a job normally waits well under a second). It always logs
    # at ERROR; set `alert_email` to also mail it, at most once per `alert_cooldown_seconds`.
    accounts_queue_stale_seconds: int = 300
    alert_email: str = ""
    alert_cooldown_seconds: int = 3_600
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # Socket timeout for the SMTP connection (connect + each command). Without
    # one, `smtplib.SMTP` blocks forever on a mail server that never responds
    # (a dropped connection with no RST, a firewall silently eating packets) —
    # and mail is sent by the arq accounts worker (app/email.py), so a hang would hold
    # one of its concurrency slots until the job's own timeout.
    smtp_timeout_seconds: float = 10.0
    # Bounded retry of the SMTP hop (worker only): `email_send_attempts` tries with an
    # exponential backoff starting at `email_retry_delay_seconds`. The worst case
    # (attempts x smtp_timeout + backoff) must fit inside the email job's timeout
    # (`EMAIL_JOB_TIMEOUT_SECONDS`, below); a test pins that.
    email_send_attempts: int = 3
    email_retry_delay_seconds: float = 2.0

    # Submission limits (DESIGN.md §4.3).
    max_code_bytes: int = 64 * 1024
    submit_rate_limit_per_minute: int = 10   # per user, POST /submissions
    run_rate_limit_per_minute: int = 15      # per user, POST /run

    @property
    def cors_origin_list(self) -> list[str]:
        """`cors_origins` parsed from a comma-separated string into a clean list.

        Env vars are strings, so a list is supplied as `"a,b"` and split here.
        """
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _totp_keys_are_valid_fernet_keys(self) -> "Settings":
        """A malformed key would only fail on first use — at some user's 2FA login. Catch
        it at startup, and under ENV=prod also refuse the public dev default."""
        from cryptography.fernet import Fernet  # local: config imports stay light

        keys = [self.totp_encryption_key] + [k.strip() for k in self.totp_previous_keys.split(",")
                                             if k.strip()]
        for key in keys:
            try:
                Fernet(key)
            except (ValueError, TypeError):
                raise ValueError(
                    "TOTP_ENCRYPTION_KEY / TOTP_PREVIOUS_KEYS must be valid Fernet keys. Generate one "
                    'with: python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"') from None
        if self.env == "prod" and DEV_TOTP_KEY in keys:
            raise ValueError("ENV=prod refuses the public dev TOTP_ENCRYPTION_KEY; set a real one")
        return self

    @model_validator(mode="after")
    def _watchdog_threshold_outlasts_a_mail_job(self) -> "Settings":
        """`ACCOUNTS_QUEUE_STALE_SECONDS` must exceed the mail job timeout.

        arq keeps a job in the queue set until it *finishes*, so the watchdog's "oldest job"
        age includes a job that is mid-run. A threshold at or below the job timeout could
        therefore fire on a perfectly healthy worker that is just slow on one send. Enforce it
        at startup rather than assume nobody tunes it that low.
        """
        if self.accounts_queue_stale_seconds <= EMAIL_JOB_TIMEOUT_SECONDS:
            raise ValueError(
                f"ACCOUNTS_QUEUE_STALE_SECONDS ({self.accounts_queue_stale_seconds}) must be "
                f"greater than the mail job timeout ({EMAIL_JOB_TIMEOUT_SECONDS}s), or a slow "
                "but healthy send would look like a stuck worker")
        return self

    @model_validator(mode="after")
    def _smtp_needs_a_host(self) -> "Settings":
        """`EMAIL_BACKEND=smtp` with no `SMTP_HOST` would only fail later, per email, in
        the worker (retried, then logged) — verify and reset links would silently never
        arrive. Fail at startup instead."""
        if self.email_backend == "smtp" and not self.smtp_host:
            raise ValueError("EMAIL_BACKEND=smtp requires SMTP_HOST")
        return self

    @model_validator(mode="after")
    def _prod_needs_real_jwt_secrets(self) -> "Settings":
        """Fail at startup if prod would trust a guessable JWT secret.

        The repo-visible dev default (or a short one) means anyone can forge a
        token for any user. That applies to *verify-only* keys too: a retired secret in
        `JWT_PREVIOUS_SECRETS` still validates tokens, so listing the dev default
        there would be the same hole. A crash at boot is louder — and cheaper —
        than discovering it after deploy. Dev and tests are untouched.
        """
        if self.env != "prod":
            return self
        keys = [self.jwt_secret] + [k.strip() for k in self.jwt_previous_secrets.split(",")
                                    if k.strip()]
        if any(k == DEV_JWT_SECRET or len(k) < MIN_PROD_JWT_SECRET_LEN for k in keys):
            raise ValueError(
                f"ENV=prod requires strong JWT secrets (>= {MIN_PROD_JWT_SECRET_LEN} chars, "
                "not the dev default) in JWT_SECRET and JWT_PREVIOUS_SECRETS. "
                "Generate one with: openssl rand -hex 32")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings`, constructed once and cached.

    `lru_cache` makes this a lazy singleton: the environment is read on the first
    call and reused everywhere, so all modules see identical config.
    """
    return Settings()
