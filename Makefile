.PHONY: demo report poll test clean

demo:
	python3 -m kanoya.cli demo

report:
	python3 -m kanoya.cli report

poll:
	python3 -m kanoya.cli poll

test:
	python3 -m pytest tests/ -q

clean:
	rm -rf dist data __pycache__ .pytest_cache
	find . -name '__pycache__' -type d -exec rm -rf {} +
