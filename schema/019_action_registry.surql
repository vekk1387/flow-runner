-- 019_action_registry.surql
-- Action registry: single source of truth for all flow actions,
-- their parameters, output keys, categories, and cost profiles.
-- Consumed by: flow-builder UI, flow-review skill, flow YAML authors.

-- ============================================================
-- action_registry: registered action definitions
-- ============================================================
DEFINE TABLE action_registry SCHEMAFULL;

DEFINE FIELD name ON action_registry TYPE string;
DEFINE FIELD category ON action_registry TYPE string
  ASSERT $value IN ['Database', 'Routing', 'Prompt', 'LLM', 'Eval', 'File'];
DEFINE FIELD cost ON action_registry TYPE string
  ASSERT $value IN ['free', 'metered', 'subscription', 'varies'];
DEFINE FIELD description ON action_registry TYPE string;
DEFINE FIELD params ON action_registry TYPE array DEFAULT [];
  -- Each element: { name: string, hint: string, required: bool }
DEFINE FIELD outputs ON action_registry TYPE array DEFAULT [];
  -- Each element: { key: string, type: string, description: string }
DEFINE FIELD active ON action_registry TYPE bool DEFAULT true;
DEFINE FIELD updated_at ON action_registry TYPE datetime DEFAULT time::now();

DEFINE INDEX idx_ar_name ON action_registry FIELDS name UNIQUE;
DEFINE INDEX idx_ar_category ON action_registry FIELDS category;
