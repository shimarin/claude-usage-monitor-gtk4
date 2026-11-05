# Claude Usage Monitor (GTK4) — build & install
#
# `make`         -> build a single self-contained executable (Python zipapp)
#                   in build/, using the system python3 + system PyGObject/GTK4.
# `make install` -> install the executable, .desktop file and icon.
# `make uninstall` / `make clean`.
#
# Install location:
#   PREFIX defaults to /usr/local when run as root, else ~/.local (no root needed).
#   Override with `make install PREFIX=/usr`.
#   DESTDIR is prepended to every path for staged installs (e.g. ebuild); when
#   DESTDIR is set the desktop/icon cache updates are skipped (handled in
#   pkg_postinst instead).

APP_ID   := com.walbrix.ClaudeUsageMonitor
BIN_NAME := claude-usage-monitor-gtk4

ifeq ($(shell id -u),0)
  PREFIX ?= /usr/local
else
  PREFIX ?= $(HOME)/.local
endif

DESTDIR ?=
BINDIR  := $(PREFIX)/bin
DATADIR := $(PREFIX)/share
APPSDIR := $(DATADIR)/applications
ICONDIR := $(DATADIR)/icons/hicolor/128x128/apps

BUILD := build
PKG   := $(BUILD)/pkg
EXE   := $(BUILD)/$(BIN_NAME)
UI    := $(BUILD)/widget.ui
ICON  := $(BIN_NAME).png

.PHONY: all build install uninstall clean test run

all: $(EXE)

# Compile the Blueprint UI to GTK builder XML.
$(UI): ui/widget.blp
	@mkdir -p $(BUILD)
	blueprint-compiler compile $< --output $@

# Assemble the sources + compiled UI into a single executable zipapp.
$(EXE): prototype.py api.py $(UI)
	@rm -rf $(PKG)
	@mkdir -p $(PKG)
	cp prototype.py $(PKG)/__main__.py
	cp api.py       $(PKG)/api.py
	cp $(UI)        $(PKG)/widget.ui
	python3 -m zipapp $(PKG) -o $(EXE) -p '/usr/bin/env python3'
	@chmod +x $(EXE)
	@echo "Built $(EXE)"

run: $(EXE)
	./$(EXE)

test:
	python3 -m unittest tests -v

install: all
	install -d $(DESTDIR)$(BINDIR) $(DESTDIR)$(APPSDIR) $(DESTDIR)$(ICONDIR)
	install -m 0755 $(EXE) $(DESTDIR)$(BINDIR)/$(BIN_NAME)
	install -m 0644 $(ICON) $(DESTDIR)$(ICONDIR)/$(APP_ID).png
	sed -e 's|@BIN@|$(BINDIR)/$(BIN_NAME)|g' \
	    -e 's|@ICON@|$(APP_ID)|g' \
	    -e 's|@APPID@|$(APP_ID)|g' \
	    claude-usage-monitor-gtk4.desktop.in > $(DESTDIR)$(APPSDIR)/$(APP_ID).desktop
	@chmod 0644 $(DESTDIR)$(APPSDIR)/$(APP_ID).desktop
ifeq ($(DESTDIR),)
	-update-desktop-database $(APPSDIR) 2>/dev/null || true
	-gtk-update-icon-cache -t -f $(DATADIR)/icons/hicolor 2>/dev/null || true
endif
	@echo "Installed to $(DESTDIR)$(PREFIX). Run '$(BIN_NAME)' (ensure $(BINDIR) is on PATH)."

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/$(BIN_NAME)
	rm -f $(DESTDIR)$(APPSDIR)/$(APP_ID).desktop
	rm -f $(DESTDIR)$(ICONDIR)/$(APP_ID).png
ifeq ($(DESTDIR),)
	-update-desktop-database $(APPSDIR) 2>/dev/null || true
	-gtk-update-icon-cache -t -f $(DATADIR)/icons/hicolor 2>/dev/null || true
endif
	@echo "Uninstalled from $(DESTDIR)$(PREFIX)."

clean:
	rm -rf $(BUILD)
