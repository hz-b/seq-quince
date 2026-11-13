SHELL := /bin/bash
VERSION ?= 0.1.0
.DEFAULT_GOAL := help

BASH_COMPLETION_DIR ?= ~/.bash_completion.d
WITH_VENV = if [ -z "$$VIRTUAL_ENV" ]; then source "$(CURDIR)/mkvenv.sh"; fi

# set COMMIT for tagging
COMMIT ?= HEAD

# Completion files expected in bash-completion/ (copy them over from monopy/bash-completion).
SCRIPTS = \
    scheduler_runner \
    device_runner
# ...

SCRIPT_PATHS = $(foreach script,$(SCRIPTS),$(BASH_COMPLETION_DIR)/$(script))

.PHONY: help all install_completion uninstall_completion clean cscope tag lint test build

help: ## Show available development targets
	@echo "seq_quince development targets"
	@echo
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / { printf "  %-22s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

all: install_completion ## Install bash completion scripts

install_completion: ## Install bash completion files and add source lines to ~/.bashrc
	@mkdir -p $(BASH_COMPLETION_DIR)
	@for script in $(SCRIPTS); do \
		if [ ! -f bash-completion/$$script ]; then echo "Skipping $$script (not found in bash-completion/)"; continue; fi; \
		cp bash-completion/$$script $(BASH_COMPLETION_DIR)/$$script; \
		grep -q "$$script" ~/.bashrc || echo "source $(BASH_COMPLETION_DIR)/$$script" >> ~/.bashrc; \
	done
	@echo "Bash completion for $(SCRIPTS) installed. Reload your shell to activate."

uninstall_completion: ## Remove installed bash completion files
	@for script in $(SCRIPTS); do \
		rm -f $(BASH_COMPLETION_DIR)/$$script; \
	done
	@echo "Bash completion for $(SCRIPTS) removed. Edit your .bashrc and remove the source lines."

clean: ## Remove local virtualenv, caches, tags, and egg-info
	@rm -rf .venv
	@rm -rf dist build
	@rm -f tags cscope.files cscope.out
	@find src -maxdepth 1 -type d -name "*.egg-info" -exec rm -rf {} +
	@find . -type d -name "__pycache__" -exec rm -r {} +

cscope: ## Build cscope and ctags indexes (optionally includes ../kiwi-scan)
	find . \( -path ./.venv -o -path ./build -o -path ./dist \) -prune \
		-o -name "*.py" -print > cscope.files
	if [ -d ../kiwi-scan ]; then \
		find ../kiwi-scan/ \
			\( -path ../kiwi-scan/.venv \
			-o -path ../kiwi-scan/build \
			-o -path ../kiwi-scan/dist \) -prune \
			-o -name "*.py" -print >> cscope.files; \
	fi
	cscope -b -i cscope.files
	ctags -R --languages=Python .

tag: ## Create a version+timestamp git tag from COMMIT
	@base="$(VERSION)"; \
	commit="$(COMMIT)"; \
	if git rev-parse "$$base" >/dev/null 2>&1; then \
		echo "ERROR: release tag $$base already exists"; \
		exit 1; \
	fi; \
	ts=$$(git show -s --format=%cd --date=format:%Y%m%d%H%M%S "$$commit"); \
	tag="$$base+$$ts"; \
	if git rev-parse "$$tag" >/dev/null 2>&1; then \
		echo "ERROR: tag $$tag already exists"; \
		exit 1; \
	fi; \
	echo "Creating tag $$tag"; \
	git tag -a "$$tag" "$$commit" -m "Release $$tag"

build: ## Build sdist and wheel into dist/
	@$(WITH_VENV); \
	python -m build

lint: ## Run pylint, ruff, and pyright (uses mkvenv.sh when needed)
	@echo
	@echo '========================================================================'
	@echo '  PYLINT'
	@echo '========================================================================'
	@$(WITH_VENV); \
	PYTHONPATH=src pylint src/seq_quince
	@echo '========================================================================'
	@echo
	@echo '========================================================================'
	@echo '  RUFF'
	@echo '========================================================================'
	@$(WITH_VENV); \
	ruff check src/seq_quince tests/
	@echo '========================================================================'
	@echo
	@echo '========================================================================'
	@echo '  PYRIGHT'
	@echo '========================================================================'
	@$(WITH_VENV); \
	pyright src/seq_quince
	@echo '========================================================================'
	@echo
	@echo 'Lint complete.'

test: ## Run the test suite (uses mkvenv.sh when needed)
	@$(WITH_VENV); \
	PYTHONPATH=src python -m pytest --cov=src/seq_quince --cov-report=term
