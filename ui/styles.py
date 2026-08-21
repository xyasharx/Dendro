# dendro/ui/styles.py
MODERN_DARK_THEME = """
/* ========================================================================= */
/* Global Reset & Base Typography with Complete Emoji Fallback Stack         */
/* ========================================================================= */
QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Cantarell", "Inter", "Segoe UI", "Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji", sans-serif;
    font-size: 13px;
    selection-background-color: #313244;
    selection-color: #89b4fa;
}

/* ========================================================================= */
/* Top Header & Search Bar                                                  */
/* ========================================================================= */
QWidget#HeaderContainer {
    background-color: #181825;
    border-bottom: 1px solid #313244;
}

QLineEdit#SearchBar {
    background-color: #11111b;
    border: 1px solid #313244;
    border-radius: 8px;
    padding: 8px 14px;
    color: #cdd6f4;
    font-family: "Cantarell", "Inter", "Segoe UI", "Noto Color Emoji", sans-serif;
    font-size: 13px;
}

QLineEdit#SearchBar:focus {
    border: 1px solid #89b4fa;
    background-color: #181825;
}

/* ========================================================================= */
/* Category Navigation Sidebar                                               */
/* ========================================================================= */
QListWidget#SidebarList {
    background-color: #11111b;
    border: none;
    border-right: 1px solid #313244;
    padding: 10px 6px;
    font-family: "Cantarell", "Inter", "Segoe UI", "Noto Color Emoji", sans-serif;
}

QListWidget#SidebarList::item {
    height: 34px;
    border-radius: 6px;
    padding-left: 8px;
    margin-bottom: 2px;
    color: #a6adc8;
    font-weight: 500;
}

QListWidget#SidebarList::item:hover {
    background-color: #181825;
    color: #cdd6f4;
}

QListWidget#SidebarList::item:selected {
    background-color: #313244;
    color: #89b4fa;
    font-weight: bold;
}

QListWidget#SidebarList::item:disabled {
    color: #6c7086;
    font-weight: 800;
    font-size: 11px;
    letter-spacing: 0.5px;
    padding-top: 10px;
    padding-bottom: 4px;
    background-color: transparent;
}

/* ========================================================================= */
/* Package Inspector Side Panel                                              */
/* ========================================================================= */
QWidget#InspectorPanel {
    background-color: #181825;
    border-left: 1px solid #313244;
}

QTabWidget#InspectorTabs::pane {
    border: 1px solid #313244;
    border-radius: 6px;
    background-color: #181825;
}

QTabBar::tab {
    background-color: #11111b;
    color: #a6adc8;
    padding: 6px 14px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
    font-size: 12px;
    font-weight: bold;
}

QTabBar::tab:selected {
    background-color: #313244;
    color: #89b4fa;
}

/* ========================================================================= */
/* Expandable Dependency Tree View                                          */
/* ========================================================================= */
QTreeView#PackageTreeView {
    background-color: #1e1e2e;
    border: none;
    outline: none;
    padding: 4px;
    show-decoration-selected: 1;
    font-family: "Cantarell", "Inter", "Segoe UI", "Noto Color Emoji", sans-serif;
}

QTreeView#PackageTreeView::item {
    border: none;
    border-radius: 4px;
}

QHeaderView::section {
    background-color: #181825;
    color: #a6adc8;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #313244;
    font-weight: bold;
    font-size: 12px;
}

QHeaderView::section:hover {
    background-color: #313244;
    color: #cdd6f4;
}

/* ========================================================================= */
/* Buttons & Badges                                                         */
/* ========================================================================= */
QPushButton {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: bold;
    color: #cdd6f4;
    font-family: "Cantarell", "Inter", "Segoe UI", "Noto Color Emoji", sans-serif;
}

QPushButton:hover {
    background-color: #45475a;
}

QPushButton:pressed {
    background-color: #585b70;
}

QPushButton#ApplyButton {
    background-color: #89b4fa;
    color: #11111b;
    border: none;
    font-weight: 700;
}

QPushButton#ApplyButton:hover {
    background-color: #b4befe;
}

QPushButton#ApplyButton:disabled {
    background-color: #313244;
    color: #585b70;
}

QPushButton#HeaderSecondaryBtn {
    background-color: #11111b;
    border: 1px solid #313244;
    color: #cdd6f4;
}

QPushButton#HeaderSecondaryBtn:hover {
    background-color: #313244;
    color: #89b4fa;
}

/* ========================================================================= */
/* Terminal & Log Drawer                                                    */
/* ========================================================================= */
QTextEdit#ConsoleOutput {
    background-color: #11111b;
    border: 1px solid #313244;
    border-radius: 8px;
    font-family: "JetBrains Mono", "Fira Code", "Noto Color Emoji", "Consolas", monospace;
    font-size: 12px;
    color: #a6adc8;
    padding: 8px;
}

/* ========================================================================= */
/* ScrollBars                                                               */
/* ========================================================================= */
QScrollBar:vertical {
    background-color: #181825;
    width: 8px;
    margin: 0px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background-color: #313244;
    min-height: 24px;
    border-radius: 4px;
}

QScrollBar::handle:vertical:hover {
    background-color: #45475a;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar:horizontal {
    height: 0px;
}

/* ========================================================================= */
/* Context Menus                                                            */
/* ========================================================================= */
QMenu {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 8px;
    padding: 6px;
    font-family: "Cantarell", "Inter", "Segoe UI", "Noto Color Emoji", sans-serif;
}

QMenu::item {
    padding: 8px 24px 8px 12px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #313244;
    color: #89b4fa;
}

/* ========================================================================= */
/* Status Bar & Splitter                                                    */
/* ========================================================================= */
QStatusBar {
    background-color: #11111b;
    border-top: 1px solid #313244;
    color: #a6adc8;
}

QSplitter::handle {
    background-color: #181825;
}
"""
