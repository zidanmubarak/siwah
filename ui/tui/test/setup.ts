// Snapshot fixtures are static; motion behavior is exercised with explicit props.
process.env.SIWAH_TUI_ANIMATIONS = "off"
// Color assertions must not depend on the CI shell or a developer's NO_COLOR.
delete process.env.NO_COLOR
delete process.env.COLORTERM
process.env.TERM = "xterm-256color"
