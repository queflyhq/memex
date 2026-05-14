from __future__ import annotations

from pathlib import Path

from platformdirs import user_data_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DATA_DIR = Path(user_data_dir("memex", "Quefly"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=_DEFAULT_DATA_DIR)
    listen: str = "127.0.0.1:7777"
    auth_token: str | None = None
    log_level: str = "INFO"

    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    # BGE/E5 family models expect instruction prefixes — query and stored docs
    # are encoded into different sub-spaces. The default below is the BGE-
    # canonical query prefix, which lifts retrieval accuracy 5–10pts on the
    # default model. For non-BGE encoders (MiniLM, plain MPNet) set both to ""
    # via MEMEX_EMBED_QUERY_PREFIX="" / MEMEX_EMBED_DOC_PREFIX="".
    embed_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embed_doc_prefix: str = ""
    # In-process LRU around embed() — same string twice in a session = one ONNX
    # call, not two. ~30ms saved per cache hit.
    embed_cache_size: int = 1024
    # L2-normalize embeddings before storage and search. Ensures cosine
    # similarity reduces to dot product (no magnitude bias). Should be True
    # for any modern sentence embedding model; setting False is for tests.
    embed_l2_normalize: bool = True

    # Cross-encoder reranker — second-stage precision filter. Defaults ON:
    # if fastembed isn't installed the loader falls back to a NoOp at startup
    # (loud-failure-or-graceful-fallback discipline), so this is safe to
    # leave default. Adds ~30 ms to reranked recalls in exchange for the
    # single biggest precision win in the pipeline.
    rerank_enabled: bool = True
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_top_k: int = 20

    # Hybrid-retrieval knobs. Promoted from hard-coded constants in
    # core/retrieval/hybrid.py so they can be tuned without code edits.
    rrf_k: int = 60                       # RRF damping; lower = top hits dominate
    confidence_floor: float = 0.05        # confidence is clamped above this in ranking
    confidence_exponent: float = 1.0      # raise to penalise low-confidence harder
    min_similarity: float = 0.0           # floor on vector score; <= 0 disables
    bm25_pool: int = 50                   # BM25 candidates merged into RRF
    vector_pool: int = 50                 # vector candidates merged into RRF
    expand_seeds: int = 5                 # seed concepts for graph expansion

    # Per-kind recency half-life (days). decision/constraint should age slowly
    # (rules don't go stale at 30d). opinion/approach age faster.
    # Resolved by core/lifecycle/decay.py:half_life_for_kind().
    decay_half_life_default_days: float = 90.0
    decay_half_life_decision_days: float = 365.0
    decay_half_life_constraint_days: float = 365.0
    decay_half_life_fact_days: float = 180.0
    decay_half_life_opinion_days: float = 30.0
    decay_half_life_approach_days: float = 60.0

    # LLM-driven query preprocessing. Both off by default — they call out
    # to Ollama / Anthropic on the recall hot path, adding 200ms-2s of
    # latency per recall. Flip on when:
    #   - You have local Ollama and want precision over latency
    #   - You're getting noisy results because user queries diverge from
    #     stored phrasing
    # core/retrieval/query.py implements both.
    query_expansion_enabled: bool = False
    query_expansion_max_alternates: int = 3
    query_expansion_max_tokens: int = 120
    hyde_enabled: bool = False
    hyde_max_tokens: int = 200

    # working_set L1 cache size. Caps how many concept ids stay "hot" in
    # the recency-biased cache between recalls. cachetools.LFUCache.
    working_set_size: int = 200

    # Consolidation promoter scheduler. Runs in a daemon background thread,
    # walking the recent episodic stream every `promote_interval_seconds`
    # and turning recurring patterns (≥ promote_min_support repeats of the
    # same recall→correction or recall→approval) into durable constraint
    # / decision nodes. Set promote_interval_seconds=0 to disable.
    promote_interval_seconds: int = 1800        # every 30 min
    promote_min_support: int = 3
    promote_lookback_events: int = 1000

    # ML auto-improve loop. Periodically mines (query, useful-result) pairs
    # from the episodic stream, runs retrieval evaluation, and writes the
    # JSONL training set when there's enough signal. The trainer (see
    # scripts/train_lora.py) consumes the JSONL — kept as a separate process
    # so torch isn't a daemon dep.
    automl_interval_seconds: int = 7200          # every 2 hours
    automl_min_pairs_to_eval: int = 50           # below this, skip eval (noisy)
    automl_min_pairs_to_train: int = 500         # below this, don't write JSONL
    automl_test_fraction: float = 0.2            # eval split
    # When the latest eval lifts precision@1 by this much vs the last run,
    # the result is logged as a learning signal for the dashboard.
    automl_significant_delta: float = 0.02

    # External-MCP enrichment scheduler. Runs daily by default — pulls
    # facts from configured upstream MCPs (Linear, Notion, GitHub, etc.)
    # into memeX's concept graph as ext::-prefixed nodes. Idempotent;
    # re-running updates by external id. Set to 0 to disable.
    enrich_interval_seconds: int = 86_400        # once a day
    enrich_config_filename: str = "enrichment.json"

    # Cleanup / forgetting scheduler. Runs daily by default — prunes old
    # low-confidence orphan concepts and old non-load-bearing events so
    # the DuckDB doesn't grow unbounded. Set to 0 to disable.
    cleanup_interval_seconds: int = 86_400       # once a day
    forget_unused_days: int = 365                # confirmed_at older than this
    forget_min_confidence: float = 0.05          # decayed-conf below this
    forget_max_edges: int = 0                    # 0 = orphans only
    episodic_ttl_days: int = 365                 # delete events older than this
    episodic_keep_minimum: int = 5_000           # always keep this many recent
    # Audit-trail event kinds that survive TTL pruning regardless of age.
    # These are the durable learning signals; deleting them breaks the
    # ability to A/B compare model versions across long horizons.
    episodic_keep_kinds: list[str] = [
        "consolidation_run",
        "pattern_promoted",
        "confidence_calibrated",
        "ml_significant_delta",
        "skill_validated",
        "user_correction",
    ]

    daemon_url: str | None = None
    pack_registry: str = "https://github.com/queflyhq/memex-skills"
    auto_bootstrap: bool = False
    bootstrap_root: Path = Field(default_factory=Path.cwd)

    @property
    def store_path(self) -> Path:
        """Unified DuckDB file holding concepts, edges, events, and vectors."""
        return self.data_dir / "memex.duckdb"

    @property
    def graph_path(self) -> Path:
        """Legacy Kuzu directory — only used by the one-shot migration helper."""
        return self.data_dir / "graph.kuzu"

    @property
    def vectors_path(self) -> Path:
        """Legacy SQLite vector file — migration source."""
        return self.data_dir / "vectors.db"

    @property
    def episodic_path(self) -> Path:
        """Legacy SQLite episodic file — migration source."""
        return self.data_dir / "episodic.db"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
        # auth_token disk fallback: pydantic only reads MEMEX_AUTH_TOKEN
        # from the environment. When the MCP client / hook subprocess is
        # spawned by Claude Code or the Wails desktop without that env
        # var set, every write 401s. The daemon writes its bearer to
        # <data_dir>/daemon.token on first run, so prefer the env var
        # (explicit / remote override) and fall back to disk locally.
        # Import here to avoid a circular: runtime_state imports config.
        if _settings.auth_token is None:
            from memex.runtime_state import read_auth_token
            _settings.auth_token = read_auth_token(_settings)
    return _settings


def reset_settings_for_testing(settings: Settings | None = None) -> None:
    """Test helper: replace the cached settings instance."""
    global _settings
    _settings = settings
