SHELL=/bin/bash
test:
	source .venv/bin/activate && python src/app_main.py README.md
