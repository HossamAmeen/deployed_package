.PHONY: help deploy deploy-sh install config

help:
	@echo "make config      - create config.yml from config.yml.example"
	@echo "make install     - install Python dependencies"
	@echo "make deploy      - run the Python deploy script"
	@echo "make deploy-sh   - run the shell deploy script"

config:
	@test -f config.yml || cp config.yml.example config.yml && echo "config.yml ready, fill it in"

install:
	pip install -r requirements.txt

deploy:
	python3 deploy.py

deploy-sh:
	./deploy.sh
