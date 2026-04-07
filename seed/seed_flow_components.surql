-- Seed data: stored queries and model configs for flow engine
-- Run after 018_flows.surql migration

-- ============================================================
-- Stored Queries
-- ============================================================

-- Agent inbox: unread messages + assigned/in-progress tasks
CREATE stored_query SET
  key = 'agent_inbox',
  description = 'Fetch unread actionable messages and active tasks for an agent',
  sql = 'SELECT id, from_agent.display_name AS sender, type, subject, body, created_at FROM message WHERE to_agent = agent:$agent_id AND status = \'unread\' AND type IN [\'request\',\'handoff\',\'escalation\',\'alert\'] ORDER BY created_at ASC; SELECT id, title, status, priority, description, domain, updated_at FROM task WHERE owner = agent:$agent_id AND status IN [\'assigned\', \'in_progress\'] ORDER BY priority ASC;',
  params = ['agent_id'];

-- Agent persona: identity, role, boundaries
CREATE stored_query SET
  key = 'agent_persona',
  description = 'Fetch agent identity record',
  sql = 'SELECT * FROM agent:$agent_id;',
  params = ['agent_id'];

-- Agent instructions: active global + agent-scoped instructions
CREATE stored_query SET
  key = 'agent_instructions',
  description = 'Fetch active instructions for an agent (global + scoped)',
  sql = 'SELECT section, title, content, priority FROM instruction WHERE (scope = \'global\' OR scope = \'agent:$agent_id\') AND active = true ORDER BY section, priority;',
  params = ['agent_id'];

-- Recent activity: last N actions by an agent
CREATE stored_query SET
  key = 'agent_recent_activity',
  description = 'Fetch recent activity log entries for an agent',
  sql = 'SELECT action, description, timestamp FROM activity_log WHERE agent = agent:$agent_id ORDER BY timestamp DESC LIMIT 10;',
  params = ['agent_id'];

-- Log activity
CREATE stored_query SET
  key = 'log_activity',
  description = 'Insert an activity log entry',
  sql = 'CREATE activity_log SET agent = agent:$agent_id, action = \'$action\', description = \'$description\', related_task = $related_task, metadata = { model: \'$model\', tokens: $tokens, duration_ms: $duration_ms, cost: $cost }, timestamp = time::now();',
  params = ['agent_id', 'action', 'description', 'related_task', 'model', 'tokens', 'duration_ms', 'cost'];

-- Log run
CREATE stored_query SET
  key = 'log_run',
  description = 'Insert a run_log entry with full metrics',
  sql = 'CREATE run_log SET agent = agent:$agent_id, task_id = $task_id, model = \'$model\', total_tokens = $total_tokens, duration_ms = $duration_ms, cost = $cost, num_turns = $num_turns, created_at = time::now();',
  params = ['agent_id', 'task_id', 'model', 'total_tokens', 'duration_ms', 'cost', 'num_turns'];

-- Complete task
CREATE stored_query SET
  key = 'complete_task',
  description = 'Mark a task as done',
  sql = 'UPDATE $task_id SET status = \'done\', completed_at = time::now();',
  params = ['task_id'];

-- Budget check
CREATE stored_query SET
  key = 'budget_status',
  description = 'Get current session budget state',
  sql = 'SELECT * FROM session:current;',
  params = [];

-- ============================================================
-- Model Configs
-- ============================================================

CREATE model_config SET
  key = 'opus',
  model_id = 'claude-opus-4-6',
  display_name = 'Claude Opus 4.6',
  input_cost_per_mtok = 15.0,
  output_cost_per_mtok = 75.0,
  max_tokens = 32000,
  tier = 'heavy';

CREATE model_config SET
  key = 'sonnet',
  model_id = 'claude-sonnet-4-6',
  display_name = 'Claude Sonnet 4.6',
  input_cost_per_mtok = 3.0,
  output_cost_per_mtok = 15.0,
  max_tokens = 16000,
  tier = 'standard';

CREATE model_config SET
  key = 'haiku',
  model_id = 'claude-haiku-4-5-20251001',
  display_name = 'Claude Haiku 4.5',
  input_cost_per_mtok = 0.8,
  output_cost_per_mtok = 4.0,
  max_tokens = 8192,
  tier = 'fast';

-- ============================================================
-- Eval Stored Queries
-- ============================================================

-- Recent flow runs for eval (joins with content)
CREATE stored_query SET
  key = 'recent_flow_runs_for_eval',
  description = 'Fetch recent completed flow runs with stored content for eval',
  sql = 'SELECT fr.*, frc.prompt_text, frc.response_text FROM flow_run AS fr LEFT JOIN flow_run_content AS frc ON frc.flow_run = fr.id WHERE fr.status = \'completed\' AND fr.id NOT IN (SELECT flow_run FROM routing_eval) ORDER BY fr.started_at DESC LIMIT $limit;',
  params = ['limit'];

-- Store routing eval results
CREATE stored_query SET
  key = 'store_routing_evals',
  description = 'Placeholder - eval storage handled by eval.store action',
  sql = 'SELECT 1;',
  params = [];
