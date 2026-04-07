/// Flow data model — mirrors the Python FlowDefinition for YAML serialization.
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

use crate::actions;

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct FlowDef {
    pub flow: String,
    pub version: u32,
    pub description: String,
    pub trigger: String,
    pub steps: Vec<FlowStep>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct FlowStep {
    pub id: String,
    pub action: String,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub params: BTreeMap<String, serde_yaml::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub audit: Option<String>,
}

pub const TRIGGERS: &[&str] = &["manual", "dispatch_go", "cron", "message_received", "event", "api"];

impl Default for FlowDef {
    fn default() -> Self {
        Self {
            flow: "new-flow".into(),
            version: 1,
            description: String::new(),
            trigger: "manual".into(),
            steps: Vec::new(),
        }
    }
}

impl FlowStep {
    /// Create a new step pre-populated with the action's required params as empty strings.
    pub fn from_action(action_name: &str, step_index: usize) -> Self {
        let mut params = BTreeMap::new();
        if let Some(def) = actions::find_action(action_name) {
            for p in def.params {
                if p.required {
                    params.insert(
                        p.name.to_string(),
                        serde_yaml::Value::String(String::new()),
                    );
                }
            }
        }
        Self {
            id: format!("step_{}", step_index + 1),
            action: action_name.to_string(),
            params,
            output: None,
            audit: None,
        }
    }
}

impl FlowDef {
    pub fn to_yaml(&self) -> Result<String, serde_yaml::Error> {
        serde_yaml::to_string(self)
    }

    pub fn from_yaml(yaml: &str) -> Result<Self, serde_yaml::Error> {
        serde_yaml::from_str(yaml)
    }
}
