mod actions;
mod app;
mod model;

fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default()
            .with_inner_size([960.0, 640.0])
            .with_title("Flow Builder"),
        ..Default::default()
    };

    eframe::run_native(
        "Flow Builder",
        options,
        Box::new(|cc| Ok(Box::new(app::FlowBuilderApp::new(cc)))),
    )
}
