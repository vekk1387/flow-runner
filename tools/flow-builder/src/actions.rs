/// Static registry of the 16 flow-runner actions and their expected parameters.

#[derive(Debug, Clone)]
pub struct ActionDef {
    pub name: &'static str,
    pub category: &'static str,
    pub cost: &'static str,
    pub description: &'static str,
    pub params: &'static [ParamDef],
}

#[derive(Debug, Clone)]
pub struct ParamDef {
    pub name: &'static str,
    pub hint: &'static str,
    pub required: bool,
}

const fn param(name: &'static str, hint: &'static str, required: bool) -> ParamDef {
    ParamDef {
        name,
        hint,
        required,
    }
}

pub const ACTIONS: &[ActionDef] = &[
    // ── Database ────────────────────────────────────────────
    ActionDef {
        name: "db.query",
        category: "Database",
        cost: "free",
        description: "Execute a stored SurrealQL query with bound parameters",
        params: &[
            param("query_key", "stored query key (string)", true),
            param("bind", "bound variables (key: value map)", false),
        ],
    },
    // ── Budget & Routing ────────────────────────────────────
    ActionDef {
        name: "budget.check",
        category: "Routing",
        cost: "free",
        description: "Check session/weekly budget via external script",
        params: &[],
    },
    ActionDef {
        name: "routing.assess",
        category: "Routing",
        cost: "free",
        description: "Classify task complexity (heuristic)",
        params: &[
            param("tasks", "task list (array)", false),
            param("messages", "message list (array)", false),
            param("persona", "agent persona (object)", false),
        ],
    },
    ActionDef {
        name: "routing.select_provider",
        category: "Routing",
        cost: "free",
        description: "Pick cheapest capable provider (Gemini > Codex > Claude)",
        params: &[
            param("assessment", "{{routing_assessment}} output", true),
            param("budget", "{{budget}} output", false),
            param("model_override", "force a model", false),
            param("provider_override", "force a provider", false),
        ],
    },
    ActionDef {
        name: "routing.scorecard",
        category: "Routing",
        cost: "free",
        description: "Compute routing quality signals (0-4 score)",
        params: &[
            param("llm_result", "{{llm_result}} output", true),
            param("routing", "{{routing}} output", true),
            param("assessment", "{{assessment}} output", true),
        ],
    },
    // ── Prompt ──────────────────────────────────────────────
    ActionDef {
        name: "prompt.build",
        category: "Prompt",
        cost: "free",
        description: "Assemble full prompt from persona, tasks, messages",
        params: &[
            param("persona", "agent persona (object)", false),
            param("instructions", "instruction list (array)", false),
            param("tasks", "task list (array)", false),
            param("messages", "message list (array)", false),
            param("recent_activity", "activity list (array)", false),
            param("assessment", "routing assessment (object)", false),
            param("model_override", "force a model tier", false),
        ],
    },
    // ── LLM Calls ───────────────────────────────────────────
    ActionDef {
        name: "llm.call",
        category: "LLM",
        cost: "metered (Claude)",
        description: "Call Claude via claude -p CLI",
        params: &[
            param("model", "haiku | sonnet | opus", true),
            param("prompt", "prompt text or {{ref}}", true),
            param("budget", "budget object", false),
            param("agent_id", "agent identifier", false),
            param("cwd_override", "working directory", false),
        ],
    },
    ActionDef {
        name: "llm.call_gemini",
        category: "LLM",
        cost: "free",
        description: "Call Gemini via HTTP API (free tier)",
        params: &[
            param("model", "gemini-flash | gemini-pro", true),
            param("prompt", "prompt text or {{ref}}", true),
            param("budget", "budget object", false),
        ],
    },
    ActionDef {
        name: "llm.call_codex",
        category: "LLM",
        cost: "subscription",
        description: "Call OpenAI via codex exec CLI",
        params: &[
            param("model", "model name", true),
            param("prompt", "prompt text or {{ref}}", true),
            param("budget", "budget object", false),
            param("cwd_override", "working directory", false),
        ],
    },
    ActionDef {
        name: "llm.call_auto",
        category: "LLM",
        cost: "varies",
        description: "Auto-dispatch with Gemini cap fallback",
        params: &[
            param("routing", "{{routing}} output", true),
            param("prompt", "prompt text or {{ref}}", true),
            param("budget", "budget object", false),
            param("agent_id", "agent identifier", false),
        ],
    },
    // ── Evaluation ──────────────────────────────────────────
    ActionDef {
        name: "eval.judge",
        category: "Eval",
        cost: "free (Gemini)",
        description: "LLM-as-judge quality evaluation",
        params: &[
            param("flow_runs", "flow run array", true),
            param("calibrate", "also run Opus judge (bool)", false),
        ],
    },
    ActionDef {
        name: "eval.store",
        category: "Eval",
        cost: "free",
        description: "Persist eval results to routing_eval table",
        params: &[
            param("results", "{{eval_results}} output", true),
        ],
    },
    // ── File & Manifest ─────────────────────────────────────
    ActionDef {
        name: "manifest.build",
        category: "File",
        cost: "free",
        description: "AST-based project skeleton (low-token summary)",
        params: &[
            param("paths", "directories to scan (array)", true),
            param("root", "project root override", false),
            param("extensions", "file extensions (.py, .yaml, ...)", false),
            param("exclude", "directories to exclude (array)", false),
        ],
    },
    ActionDef {
        name: "file.read",
        category: "File",
        cost: "free",
        description: "Read one or more files",
        params: &[
            param("paths", "file path(s) (string or array)", true),
            param("root", "root directory", false),
            param("line_range", "{start, end} line range", false),
        ],
    },
    ActionDef {
        name: "file.write",
        category: "File",
        cost: "free",
        description: "Write content to a file",
        params: &[
            param("path", "target file path", true),
            param("content", "file content", true),
            param("mode", "overwrite | append", false),
            param("root", "root directory", false),
        ],
    },
    ActionDef {
        name: "context.select",
        category: "File",
        cost: "free",
        description: "Extract files from manifest based on LLM selection",
        params: &[
            param("manifest", "{{manifest}} output", true),
            param("selection", "selected paths (list/dict/string)", true),
            param("root", "root directory", false),
        ],
    },
];

/// All unique category names, in display order.
pub const CATEGORIES: &[&str] = &["Database", "Routing", "Prompt", "LLM", "Eval", "File"];

/// Lookup an action definition by name.
pub fn find_action(name: &str) -> Option<&'static ActionDef> {
    ACTIONS.iter().find(|a| a.name == name)
}
