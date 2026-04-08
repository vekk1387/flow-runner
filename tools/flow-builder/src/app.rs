use eframe::egui;
use std::path::PathBuf;

use crate::actions::{self, ActionDef, ACTIONS, CATEGORIES};
use crate::model::{FlowDef, FlowStep, TRIGGERS};

pub struct FlowBuilderApp {
    flow: FlowDef,
    /// Which step index is currently expanded for editing (None = all collapsed).
    editing_step: Option<usize>,
    /// Status message shown at the bottom.
    status: String,
    /// Last saved/loaded file path.
    file_path: Option<PathBuf>,
    /// Temporary buffer for adding a new param key to a step.
    new_param_key: String,
    /// YAML preview content (Some = overlay visible).
    yaml_preview: Option<String>,
}

impl Default for FlowBuilderApp {
    fn default() -> Self {
        Self {
            flow: FlowDef::default(),
            editing_step: None,
            status: "Ready — add steps from the action palette".into(),
            file_path: None,
            new_param_key: String::new(),
            yaml_preview: None,
        }
    }
}

impl FlowBuilderApp {
    pub fn new(cc: &eframe::CreationContext<'_>) -> Self {
        configure_fonts(&cc.egui_ctx);
        Self::default()
    }

    // ── File I/O ────────────────────────────────────────────

    fn save_yaml(&mut self) {
        let path = if let Some(p) = &self.file_path {
            Some(p.clone())
        } else {
            rfd::FileDialog::new()
                .set_title("Save Flow YAML")
                .add_filter("YAML", &["yaml", "yml"])
                .set_file_name(&format!("{}.yaml", self.flow.flow))
                .save_file()
        };
        if let Some(path) = path {
            match self.flow.to_yaml() {
                Ok(yaml) => match std::fs::write(&path, &yaml) {
                    Ok(_) => {
                        self.status = format!("Saved to {}", path.display());
                        self.file_path = Some(path);
                    }
                    Err(e) => self.status = format!("Write error: {e}"),
                },
                Err(e) => self.status = format!("YAML error: {e}"),
            }
        }
    }

    fn load_yaml(&mut self) {
        let path = rfd::FileDialog::new()
            .set_title("Open Flow YAML")
            .add_filter("YAML", &["yaml", "yml"])
            .pick_file();
        if let Some(path) = path {
            match std::fs::read_to_string(&path) {
                Ok(contents) => match FlowDef::from_yaml(&contents) {
                    Ok(flow) => {
                        self.flow = flow;
                        self.editing_step = None;
                        self.status = format!("Loaded {}", path.display());
                        self.file_path = Some(path);
                    }
                    Err(e) => self.status = format!("Parse error: {e}"),
                },
                Err(e) => self.status = format!("Read error: {e}"),
            }
        }
    }

    // ── UI Panels ───────────────────────────────────────────

    fn header_panel(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label("Flow:");
            ui.text_edit_singleline(&mut self.flow.flow);
            ui.label("v");
            ui.add(egui::DragValue::new(&mut self.flow.version).range(1..=99));
            ui.label("Trigger:");
            egui::ComboBox::from_id_salt("trigger")
                .selected_text(&self.flow.trigger)
                .show_ui(ui, |ui| {
                    for t in TRIGGERS {
                        ui.selectable_value(&mut self.flow.trigger, t.to_string(), *t);
                    }
                });
        });
        ui.horizontal(|ui| {
            ui.label("Description:");
            ui.add(
                egui::TextEdit::singleline(&mut self.flow.description)
                    .desired_width(f32::INFINITY),
            );
        });
    }

    fn action_palette(&mut self, ui: &mut egui::Ui) {
        ui.heading("Actions");
        ui.separator();

        for &cat in CATEGORIES {
            ui.collapsing(cat, |ui| {
                for action in ACTIONS.iter().filter(|a| a.category == cat) {
                    let label = format!("{} ({})", action.name, action.cost);
                    if ui
                        .button(&label)
                        .on_hover_text(action.description)
                        .clicked()
                    {
                        let idx = self.flow.steps.len();
                        self.flow
                            .steps
                            .push(FlowStep::from_action(action.name, idx));
                        self.editing_step = Some(idx);
                        self.status = format!("Added step: {}", action.name);
                    }
                }
            });
        }
    }

    fn step_pipeline(&mut self, ui: &mut egui::Ui) {
        ui.heading("Pipeline");
        ui.separator();

        if self.flow.steps.is_empty() {
            ui.label("No steps yet. Click an action to add one.");
            return;
        }

        let mut remove_idx: Option<usize> = None;
        let mut swap: Option<(usize, usize)> = None;
        let step_count = self.flow.steps.len();

        for i in 0..step_count {
            let step = &self.flow.steps[i];
            let action_def = actions::find_action(&step.action);
            let header = format!(
                "{}. [{}]  id: {}",
                i + 1,
                step.action,
                step.id
            );

            let is_editing = self.editing_step == Some(i);

            egui::CollapsingHeader::new(&header)
                .id_salt(format!("step_{i}"))
                .default_open(is_editing)
                .show(ui, |ui| {
                    self.step_editor(ui, i, action_def);

                    ui.horizontal(|ui| {
                        if i > 0 && ui.small_button("\u{2191} Up").clicked() {
                            swap = Some((i, i - 1));
                        }
                        if i + 1 < step_count && ui.small_button("\u{2193} Down").clicked() {
                            swap = Some((i, i + 1));
                        }
                        if ui
                            .small_button("\u{2717} Remove")
                            .on_hover_text("Delete this step")
                            .clicked()
                        {
                            remove_idx = Some(i);
                        }
                    });
                });
        }

        // Apply mutations after iteration
        if let Some((a, b)) = swap {
            self.flow.steps.swap(a, b);
        }
        if let Some(idx) = remove_idx {
            self.flow.steps.remove(idx);
            self.editing_step = None;
            self.status = "Step removed".into();
        }
    }

    fn step_editor(&mut self, ui: &mut egui::Ui, idx: usize, action_def: Option<&ActionDef>) {
        let step = &mut self.flow.steps[idx];

        // Step ID
        ui.horizontal(|ui| {
            ui.label("id:");
            ui.text_edit_singleline(&mut step.id);
        });

        // Output variable
        let mut has_output = step.output.is_some();
        ui.horizontal(|ui| {
            ui.checkbox(&mut has_output, "output:");
            if has_output {
                let output = step.output.get_or_insert_with(String::new);
                ui.text_edit_singleline(output);
            } else {
                step.output = None;
            }
        });

        // Audit flag
        let mut has_audit = step.audit.is_some();
        ui.horizontal(|ui| {
            ui.checkbox(&mut has_audit, "audit:");
            if has_audit {
                step.audit.get_or_insert_with(|| "full".into());
                ui.label("full");
            } else {
                step.audit = None;
            }
        });

        // Parameters
        ui.separator();
        ui.label("Params:");

        // Show hints for known action params
        if let Some(def) = action_def {
            for p in def.params {
                let entry = step.params.entry(p.name.to_string());
                let val = entry.or_insert_with(|| serde_yaml::Value::String(String::new()));
                ui.horizontal(|ui| {
                    let label = if p.required {
                        format!("{}*:", p.name)
                    } else {
                        format!("{}:", p.name)
                    };
                    ui.label(&label);
                    // Edit as string representation
                    let mut s = yaml_value_to_string(val);
                    if ui
                        .add(
                            egui::TextEdit::singleline(&mut s)
                                .desired_width(f32::INFINITY)
                                .hint_text(p.hint),
                        )
                        .changed()
                    {
                        *val = string_to_yaml_value(&s);
                    }
                });
            }
        }

        // Extra/custom params not in the action def
        let known_params: Vec<String> = action_def
            .map(|d| d.params.iter().map(|p| p.name.to_string()).collect())
            .unwrap_or_default();

        let extra_keys: Vec<String> = step
            .params
            .keys()
            .filter(|k| !known_params.contains(k))
            .cloned()
            .collect();

        for key in &extra_keys {
            if let Some(val) = step.params.get_mut(key) {
                ui.horizontal(|ui| {
                    ui.label(format!("{key}:"));
                    let mut s = yaml_value_to_string(val);
                    if ui
                        .add(egui::TextEdit::singleline(&mut s).desired_width(f32::INFINITY))
                        .changed()
                    {
                        *val = string_to_yaml_value(&s);
                    }
                });
            }
        }

        // Add custom param
        ui.horizontal(|ui| {
            ui.label("+");
            ui.add(
                egui::TextEdit::singleline(&mut self.new_param_key)
                    .desired_width(120.0)
                    .hint_text("new param key"),
            );
            if ui.small_button("Add").clicked() && !self.new_param_key.is_empty() {
                step.params.insert(
                    self.new_param_key.clone(),
                    serde_yaml::Value::String(String::new()),
                );
                self.new_param_key.clear();
            }
        });
    }

    fn bottom_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            if ui.button("\u{1F4BE} Save YAML").clicked() {
                self.save_yaml();
            }
            if ui.button("\u{1F4C2} Open YAML").clicked() {
                self.load_yaml();
            }
            if ui.button("\u{1F4CB} Preview YAML").clicked() {
                match self.flow.to_yaml() {
                    Ok(yaml) => self.yaml_preview = Some(yaml),
                    Err(e) => self.status = format!("YAML error: {e}"),
                }
            }
            if ui.button("New Flow").clicked() {
                self.flow = FlowDef::default();
                self.editing_step = None;
                self.file_path = None;
                self.status = "New flow created".into();
            }
            ui.separator();
            ui.label(format!(
                "{} steps | trigger: {}",
                self.flow.steps.len(),
                self.flow.trigger
            ));
        });
    }
}

impl eframe::App for FlowBuilderApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // Top panel: flow metadata
        egui::TopBottomPanel::top("header").show(ctx, |ui| {
            self.header_panel(ui);
        });

        // Bottom panel: actions + status
        egui::TopBottomPanel::bottom("footer").show(ctx, |ui| {
            self.bottom_bar(ui);
            ui.separator();
            ui.label(&self.status);
        });

        // YAML preview overlay
        if self.yaml_preview.is_some() {
            egui::Window::new("YAML Preview")
                .collapsible(false)
                .resizable(true)
                .default_size([500.0, 400.0])
                .anchor(egui::Align2::CENTER_CENTER, [0.0, 0.0])
                .show(ctx, |ui| {
                    if ui.button("Close").clicked() {
                        self.yaml_preview = None;
                        return;
                    }
                    ui.separator();
                    egui::ScrollArea::vertical().show(ui, |ui| {
                        let yaml = self.yaml_preview.as_deref().unwrap_or("");
                        ui.add(
                            egui::TextEdit::multiline(&mut yaml.to_string())
                                .desired_width(f32::INFINITY)
                                .font(egui::TextStyle::Monospace),
                        );
                    });
                });
        }

        // Left panel: action palette
        egui::SidePanel::left("actions")
            .default_width(200.0)
            .show(ctx, |ui| {
                egui::ScrollArea::vertical().show(ui, |ui| {
                    self.action_palette(ui);
                });
            });

        // Central panel: step pipeline
        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                self.step_pipeline(ui);
            });
        });
    }
}

// ── Helpers ─────────────────────────────────────────────────

fn configure_fonts(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    style.text_styles.insert(
        egui::TextStyle::Body,
        egui::FontId::new(14.0, egui::FontFamily::Proportional),
    );
    style.text_styles.insert(
        egui::TextStyle::Monospace,
        egui::FontId::new(13.0, egui::FontFamily::Monospace),
    );
    ctx.set_style(style);
}

fn yaml_value_to_string(val: &serde_yaml::Value) -> String {
    match val {
        serde_yaml::Value::String(s) => s.clone(),
        serde_yaml::Value::Bool(b) => b.to_string(),
        serde_yaml::Value::Number(n) => n.to_string(),
        serde_yaml::Value::Null => String::new(),
        // For sequences/mappings, show inline YAML
        other => serde_yaml::to_string(other).unwrap_or_default().trim().to_string(),
    }
}

fn string_to_yaml_value(s: &str) -> serde_yaml::Value {
    let trimmed = s.trim();
    if trimmed.is_empty() {
        return serde_yaml::Value::String(String::new());
    }
    // Try to parse as YAML (handles booleans, numbers, arrays, objects)
    // But preserve template refs like {{foo}} as strings
    if trimmed.contains("{{") {
        return serde_yaml::Value::String(s.to_string());
    }
    serde_yaml::from_str(trimmed).unwrap_or_else(|_| serde_yaml::Value::String(s.to_string()))
}
