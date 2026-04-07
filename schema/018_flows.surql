-- 018_flows.surql
-- Flow engine tables: declarative YAML pipeline support
-- Adds: flow, flow_run, stored_query, model_config

-- ============================================================
-- flow: registered flow definitions (synced from YAML on disk)
-- ============================================================
DEFINE TABLE flow SCHEMAFULL;

DEFINE FIELD name ON flow TYPE string;
DEFINE FIELD version ON flow TYPE int DEFAULT 1;
DEFINE FIELD description ON flow TYPE option<string>;
DEFINE FIELD trigger ON flow TYPE string
  ASSERT $value IN ['dispatch_go', 'cron', 'message_received', 'manual', 'event'];
DEFINE FIELD step_count ON flow TYPE int;
DEFINE FIELD yaml_hash ON flow TYPE string;
DEFINE FIELD synced_at ON flow TYPE datetime DEFAULT time::now();
DEFINE FIELD active ON flow TYPE bool DEFAULT true;

DEFINE INDEX idx_flow_name ON flow FIELDS name UNIQUE;
DEFINE INDEX idx_flow_active ON flow FIELDS active;

-- ============================================================
-- flow_run: execution audit trail (one record per flow execution)
-- ============================================================
DEFINE TABLE flow_run SCHEMAFULL;

DEFINE FIELD flow ON flow_run TYPE record<flow>;
DEFINE FIELD flow_name ON flow_run TYPE string;
DEFINE FIELD agent ON flow_run TYPE record<agent>;
DEFINE FIELD trigger ON flow_run TYPE string;
DEFINE FIELD status ON flow_run TYPE string DEFAULT 'running'
  ASSERT $value IN ['running', 'completed', 'failed', 'cancelled'];
DEFINE FIELD started_at ON flow_run TYPE datetime DEFAULT time::now();
DEFINE FIELD ended_at ON flow_run TYPE option<datetime>;
DEFINE FIELD duration_ms ON flow_run TYPE option<int>;
DEFINE FIELD total_tokens ON flow_run TYPE option<int> DEFAULT 0;
DEFINE FIELD total_cost ON flow_run TYPE option<float> DEFAULT 0.0;
DEFINE FIELD error ON flow_run TYPE option<string>;
DEFINE FIELD provider ON flow_run TYPE option<string>;
DEFINE FIELD context ON flow_run TYPE option<object>;

DEFINE INDEX idx_frun_flow ON flow_run FIELDS flow;
DEFINE INDEX idx_frun_agent ON flow_run FIELDS agent;
DEFINE INDEX idx_frun_status ON flow_run FIELDS status;
DEFINE INDEX idx_frun_started ON flow_run FIELDS started_at;

-- ============================================================
-- flow_step_log: per-step audit within a flow run
-- ============================================================
DEFINE TABLE flow_step_log SCHEMAFULL;

DEFINE FIELD flow_run ON flow_step_log TYPE record<flow_run>;
DEFINE FIELD step_id ON flow_step_log TYPE string;
DEFINE FIELD action ON flow_step_log TYPE string;
DEFINE FIELD seq ON flow_step_log TYPE int;
DEFINE FIELD status ON flow_step_log TYPE string DEFAULT 'running'
  ASSERT $value IN ['running', 'ok', 'error', 'skipped'];
DEFINE FIELD started_at ON flow_step_log TYPE datetime DEFAULT time::now();
DEFINE FIELD ended_at ON flow_step_log TYPE option<datetime>;
DEFINE FIELD duration_ms ON flow_step_log TYPE option<int>;
DEFINE FIELD input_hash ON flow_step_log TYPE option<string>;
DEFINE FIELD output_summary ON flow_step_log TYPE option<string>;
DEFINE FIELD error ON flow_step_log TYPE option<string>;
DEFINE FIELD tokens ON flow_step_log TYPE option<int>;
DEFINE FIELD cost ON flow_step_log TYPE option<float>;
DEFINE FIELD llm_response ON flow_step_log TYPE option<string>;

DEFINE INDEX idx_fsl_run ON flow_step_log FIELDS flow_run;
DEFINE INDEX idx_fsl_action ON flow_step_log FIELDS action;

-- ============================================================
-- stored_query: parameterized SurrealQL templates referenced by key
-- ============================================================
DEFINE TABLE stored_query SCHEMAFULL;

DEFINE FIELD key ON stored_query TYPE string;
DEFINE FIELD description ON stored_query TYPE option<string>;
DEFINE FIELD sql ON stored_query TYPE string;
DEFINE FIELD params ON stored_query TYPE option<array>;
DEFINE FIELD created_at ON stored_query TYPE datetime DEFAULT time::now();
DEFINE FIELD updated_at ON stored_query TYPE datetime DEFAULT time::now();

DEFINE INDEX idx_sq_key ON stored_query FIELDS key UNIQUE;

-- ============================================================
-- model_config: LLM model parameters and cost rates
-- ============================================================
DEFINE TABLE model_config SCHEMAFULL;

DEFINE FIELD key ON model_config TYPE string;
DEFINE FIELD model_id ON model_config TYPE string;
DEFINE FIELD display_name ON model_config TYPE string;
DEFINE FIELD input_cost_per_mtok ON model_config TYPE float;
DEFINE FIELD output_cost_per_mtok ON model_config TYPE float;
DEFINE FIELD max_tokens ON model_config TYPE option<int>;
DEFINE FIELD tier ON model_config TYPE string
  ASSERT $value IN ['fast', 'standard', 'heavy'];
DEFINE FIELD active ON model_config TYPE bool DEFAULT true;

DEFINE INDEX idx_mc_key ON model_config FIELDS key UNIQUE;
DEFINE INDEX idx_mc_tier ON model_config FIELDS tier;
