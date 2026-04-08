-- seed_action_registry.surql
-- Seed all 16 flow actions with params AND output keys.

-- ── Database ───────────────────────────────────────────────

CREATE action_registry SET
  name = 'db.query',
  category = 'Database',
  cost = 'free',
  description = 'Execute a stored SurrealQL query with bound parameters',
  params = [
    { name: 'query_key', hint: 'stored query key (string)', required: true },
    { name: 'bind', hint: 'bound variables (key: value map)', required: false }
  ],
  outputs = [
    { key: '(dynamic)', type: 'object | array', description: 'Shape depends on the stored query. Single-row queries return a dict, multi-row return a list.' }
  ];

-- ── Routing ────────────────────────────────────────────────

CREATE action_registry SET
  name = 'budget.check',
  category = 'Routing',
  cost = 'free',
  description = 'Check session/weekly budget via external script',
  params = [],
  outputs = [
    { key: 'status', type: 'string', description: '"ok" or "blocked"' },
    { key: 'session_pct', type: 'float', description: 'Session budget usage percentage' },
    { key: 'weekly_pct', type: 'float', description: 'Weekly budget usage percentage' },
    { key: 'adaptive_cap', type: 'int', description: 'Adaptive token cap' },
    { key: 'reason', type: 'string', description: 'Reason for block (only when status=blocked)' },
    { key: 'raw', type: 'string', description: 'Raw script output (only when status=ok)' }
  ];

CREATE action_registry SET
  name = 'routing.assess',
  category = 'Routing',
  cost = 'free',
  description = 'Classify task complexity and determine model tier (heuristic, no LLM)',
  params = [
    { name: 'tasks', hint: 'task list (array)', required: false },
    { name: 'messages', hint: 'message list (array)', required: false },
    { name: 'persona', hint: 'agent persona (object)', required: false }
  ],
  outputs = [
    { key: 'complexity', type: 'string', description: '"none" | "low" | "medium" | "high"' },
    { key: 'model_tier', type: 'string', description: '"fast" | "standard" | "heavy"' },
    { key: 'estimated_tokens', type: 'int', description: 'Estimated token usage' },
    { key: 'needs_tool_use', type: 'bool', description: 'Task requires tool use (file ops, git, etc.)' },
    { key: 'needs_repo_context', type: 'bool', description: 'Task requires repo context (config, schema, etc.)' },
    { key: 'is_code_heavy', type: 'bool', description: 'Task is code-heavy (implement, module, etc.)' },
    { key: 'reason', type: 'string', description: 'Human-readable reason for classification' }
  ];

CREATE action_registry SET
  name = 'routing.select_provider',
  category = 'Routing',
  cost = 'free',
  description = 'Pick cheapest capable provider: Gemini (free) > Codex (subscription) > Claude (metered)',
  params = [
    { name: 'assessment', hint: '{{routing_assessment}} output', required: true },
    { name: 'budget', hint: '{{budget}} output', required: false },
    { name: 'model_override', hint: 'force a specific model', required: false },
    { name: 'provider_override', hint: 'force a specific provider', required: false }
  ],
  outputs = [
    { key: 'provider', type: 'string', description: '"gemini" | "codex" | "claude"' },
    { key: 'action', type: 'string', description: 'The llm action to call (e.g., "llm.call_gemini")' },
    { key: 'model', type: 'string', description: 'Model name to use' },
    { key: 'reason', type: 'string', description: 'Human-readable routing reason' },
    { key: 'cost_rank', type: 'int', description: '0=free, 1=subscription, 2=metered' }
  ];

CREATE action_registry SET
  name = 'routing.scorecard',
  category = 'Routing',
  cost = 'free',
  description = 'Compute routing quality signals (score 0-4). No LLM call — pure math.',
  params = [
    { name: 'llm_result', hint: '{{llm_result}} output', required: true },
    { name: 'routing', hint: '{{routing}} output', required: true },
    { name: 'assessment', hint: '{{assessment}} output', required: true }
  ],
  outputs = [
    { key: 'score', type: 'int', description: 'Composite score 0-4' },
    { key: 'max_score', type: 'int', description: 'Always 4' },
    { key: 'call_succeeded', type: 'bool', description: 'Call completed without error' },
    { key: 'produced_output', type: 'bool', description: 'Output tokens > 50' },
    { key: 'engaged', type: 'bool', description: 'Engagement ratio > 2%' },
    { key: 'engagement_ratio', type: 'float', description: 'output_tokens / input_tokens' },
    { key: 'fell_back', type: 'bool', description: 'Provider fallback was triggered' },
    { key: 'likely_misrouted', type: 'bool', description: 'Call succeeded but produced almost nothing' },
    { key: 'provider', type: 'string', description: 'Provider that was used' },
    { key: 'model', type: 'string', description: 'Model that was used' },
    { key: 'complexity', type: 'string', description: 'Assessed complexity' },
    { key: 'cost_rank', type: 'int', description: 'Cost rank from routing' }
  ];

-- ── Prompt ──────────────────────────────────────────────────

CREATE action_registry SET
  name = 'prompt.build',
  category = 'Prompt',
  cost = 'free',
  description = 'Assemble full prompt payload from persona, instructions, tasks, messages, and activity',
  params = [
    { name: 'persona', hint: 'agent persona (object)', required: false },
    { name: 'instructions', hint: 'instruction list (array)', required: false },
    { name: 'tasks', hint: 'task list (array)', required: false },
    { name: 'messages', hint: 'message list (array)', required: false },
    { name: 'recent_activity', hint: 'activity list (array)', required: false },
    { name: 'assessment', hint: 'routing assessment (object)', required: false },
    { name: 'model_override', hint: 'force a model tier', required: false }
  ],
  outputs = [
    { key: 'user_prompt', type: 'string', description: 'The assembled prompt text' },
    { key: 'model', type: 'string', description: 'Model flag (haiku/sonnet/opus)' },
    { key: 'model_tier', type: 'string', description: 'Model tier (fast/standard/heavy)' }
  ];

-- ── LLM ─────────────────────────────────────────────────────

CREATE action_registry SET
  name = 'llm.call',
  category = 'LLM',
  cost = 'metered',
  description = 'Call Claude via claude -p CLI',
  params = [
    { name: 'model', hint: 'haiku | sonnet | opus', required: true },
    { name: 'prompt', hint: 'prompt text or {{ref}}', required: true },
    { name: 'budget', hint: 'budget object', required: false },
    { name: 'agent_id', hint: 'agent identifier', required: false },
    { name: 'cwd_override', hint: 'working directory', required: false }
  ],
  outputs = [
    { key: 'provider', type: 'string', description: 'Always "claude"' },
    { key: 'text', type: 'string', description: 'LLM response text' },
    { key: 'model', type: 'string', description: 'Model used' },
    { key: 'stop_reason', type: 'string', description: 'end_turn | error | budget_blocked | rate_limited | timeout' },
    { key: 'is_error', type: 'bool', description: 'Whether the call errored' },
    { key: 'total_tokens', type: 'int', description: 'Total token count' },
    { key: 'input_tokens', type: 'int', description: 'Input token count' },
    { key: 'output_tokens', type: 'int', description: 'Output token count' },
    { key: 'cost', type: 'float', description: 'Cost in USD' },
    { key: 'duration_ms', type: 'int', description: 'Wall-clock duration' },
    { key: 'num_turns', type: 'int', description: 'Number of conversation turns' }
  ];

CREATE action_registry SET
  name = 'llm.call_gemini',
  category = 'LLM',
  cost = 'free',
  description = 'Call Gemini via HTTP API (free tier)',
  params = [
    { name: 'model', hint: 'gemini-flash | gemini-pro', required: true },
    { name: 'prompt', hint: 'prompt text or {{ref}}', required: true },
    { name: 'budget', hint: 'budget object', required: false }
  ],
  outputs = [
    { key: 'provider', type: 'string', description: 'Always "gemini"' },
    { key: 'text', type: 'string', description: 'LLM response text' },
    { key: 'model', type: 'string', description: 'Model version returned by API' },
    { key: 'stop_reason', type: 'string', description: 'Finish reason from Gemini' },
    { key: 'is_error', type: 'bool', description: 'Whether the call errored' },
    { key: 'total_tokens', type: 'int', description: 'Total token count' },
    { key: 'input_tokens', type: 'int', description: 'Input token count' },
    { key: 'output_tokens', type: 'int', description: 'Output token count' },
    { key: 'thinking_tokens', type: 'int', description: 'Thinking token count (if applicable)' },
    { key: 'cost', type: 'float', description: 'Always 0.0 (free tier)' },
    { key: 'duration_ms', type: 'int', description: 'Wall-clock duration' },
    { key: 'gemini_capped', type: 'bool', description: 'True if rate limited (triggers fallback in llm.call_auto)' }
  ];

CREATE action_registry SET
  name = 'llm.call_codex',
  category = 'LLM',
  cost = 'subscription',
  description = 'Call OpenAI via codex exec CLI',
  params = [
    { name: 'model', hint: 'model name', required: true },
    { name: 'prompt', hint: 'prompt text or {{ref}}', required: true },
    { name: 'budget', hint: 'budget object', required: false },
    { name: 'cwd_override', hint: 'working directory', required: false }
  ],
  outputs = [
    { key: 'provider', type: 'string', description: 'Always "codex"' },
    { key: 'text', type: 'string', description: 'LLM response text' },
    { key: 'model', type: 'string', description: 'Model used' },
    { key: 'stop_reason', type: 'string', description: 'end_turn or error' },
    { key: 'is_error', type: 'bool', description: 'Whether the call errored' },
    { key: 'total_tokens', type: 'int', description: 'Total token count' },
    { key: 'input_tokens', type: 'int', description: 'Input token count' },
    { key: 'output_tokens', type: 'int', description: 'Output token count' },
    { key: 'cost', type: 'float', description: 'Always 0.0 (subscription)' },
    { key: 'duration_ms', type: 'int', description: 'Wall-clock duration' },
    { key: 'num_turns', type: 'int', description: 'Number of conversation turns' }
  ];

CREATE action_registry SET
  name = 'llm.call_auto',
  category = 'LLM',
  cost = 'varies',
  description = 'Auto-dispatch to cheapest viable provider with Gemini rate-limit fallback',
  params = [
    { name: 'routing', hint: '{{routing}} output', required: true },
    { name: 'prompt', hint: 'prompt text or {{ref}}', required: true },
    { name: 'budget', hint: 'budget object', required: false },
    { name: 'agent_id', hint: 'agent identifier', required: false },
    { name: 'cwd_override', hint: 'working directory', required: false }
  ],
  outputs = [
    { key: '(all keys from underlying provider)', type: '(varies)', description: 'Inherits all output keys from llm.call, llm.call_gemini, or llm.call_codex' },
    { key: '_routing', type: 'object', description: 'Routing decision including fallback_from, fallback_to, fallback_reason if fallback occurred' }
  ];

-- ── Eval ────────────────────────────────────────────────────

CREATE action_registry SET
  name = 'eval.judge',
  category = 'Eval',
  cost = 'free',
  description = 'LLM-as-judge quality evaluation. Uses Gemini Pro (free), optional Opus calibration.',
  params = [
    { name: 'flow_runs', hint: 'flow run array with stored content', required: true },
    { name: 'calibrate', hint: 'also run Opus judge (bool)', required: false }
  ],
  outputs = [
    { key: 'evaluated', type: 'int', description: 'Number of runs evaluated' },
    { key: 'calibrated', type: 'bool', description: 'Whether Opus calibration was run' },
    { key: 'results', type: 'array', description: 'List of eval entries, each with gemini_pro and optionally opus sub-dicts containing quality (int), could_simpler_handle (bool), was_misrouted (bool), reasoning (string)' }
  ];

CREATE action_registry SET
  name = 'eval.store',
  category = 'Eval',
  cost = 'free',
  description = 'Persist eval results to routing_eval table',
  params = [
    { name: 'results', hint: '{{eval_results}} output from eval.judge', required: true }
  ],
  outputs = [
    { key: 'stored', type: 'int', description: 'Number of entries stored' },
    { key: 'total_entries', type: 'int', description: 'Total entries processed' }
  ];

-- ── File & Manifest ─────────────────────────────────────────

CREATE action_registry SET
  name = 'manifest.build',
  category = 'File',
  cost = 'free',
  description = 'AST-based project skeleton. Scans Python, YAML, SQL, TOML for structure — no file contents.',
  params = [
    { name: 'paths', hint: 'directories to scan (array)', required: true },
    { name: 'root', hint: 'project root override', required: false },
    { name: 'extensions', hint: 'file extensions to include (.py, .yaml, ...)', required: false },
    { name: 'exclude', hint: 'directories to exclude (array)', required: false }
  ],
  outputs = [
    { key: 'text', type: 'string', description: 'The manifest as formatted text' },
    { key: 'files', type: 'array', description: 'List of file entry dicts with path, type, etc.' },
    { key: 'file_count', type: 'int', description: 'Number of files scanned' },
    { key: 'token_estimate', type: 'int', description: 'Estimated token count of manifest text' }
  ];

CREATE action_registry SET
  name = 'file.read',
  category = 'File',
  cost = 'free',
  description = 'Read one or more files and return their contents',
  params = [
    { name: 'paths', hint: 'file path(s) relative to root (string or array)', required: true },
    { name: 'root', hint: 'root directory override', required: false },
    { name: 'line_range', hint: '{ start, end } line range', required: false }
  ],
  outputs = [
    { key: 'files', type: 'object', description: 'Dict mapping path → { content, lines, chars } or { error }' },
    { key: 'file_count', type: 'int', description: 'Number of files read' },
    { key: 'total_chars', type: 'int', description: 'Total characters across all files' },
    { key: 'token_estimate', type: 'int', description: 'Estimated token count (chars / 4)' }
  ];

CREATE action_registry SET
  name = 'file.write',
  category = 'File',
  cost = 'free',
  description = 'Write content to a file (overwrite or append)',
  params = [
    { name: 'path', hint: 'target file path relative to root', required: true },
    { name: 'content', hint: 'file content to write', required: true },
    { name: 'mode', hint: 'overwrite (default) | append', required: false },
    { name: 'root', hint: 'root directory override', required: false }
  ],
  outputs = [
    { key: 'path', type: 'string', description: 'File path written' },
    { key: 'status', type: 'string', description: '"written" or "dry_run"' },
    { key: 'chars', type: 'int', description: 'Characters written' },
    { key: 'lines', type: 'int', description: 'Lines written' }
  ];

CREATE action_registry SET
  name = 'context.select',
  category = 'File',
  cost = 'free',
  description = 'Read only the files an LLM identified as relevant from a manifest',
  params = [
    { name: 'manifest', hint: '{{manifest}} output from manifest.build', required: true },
    { name: 'selection', hint: 'selected file paths (list, dict with read/modify keys, or raw text)', required: true },
    { name: 'root', hint: 'root directory override', required: false }
  ],
  outputs = [
    { key: 'selected_paths', type: 'array', description: 'Valid paths that were selected and read' },
    { key: 'invalid_paths', type: 'array', description: 'Paths not found in manifest' },
    { key: 'files', type: 'object', description: 'Dict mapping path → file content' },
    { key: 'file_count', type: 'int', description: 'Number of files selected' },
    { key: 'token_estimate', type: 'int', description: 'Estimated token count of selected files' }
  ];
