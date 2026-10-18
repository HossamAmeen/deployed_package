ENV ?= staging

.PHONY: help deploy deploy-sh restart install config

help:
	@echo "make config      - overwrite config.yml with config.yml.example"
	@echo "make install     - install Python dependencies"
	@echo "make deploy      - deploy (ENV=staging|production, default staging)"
	@echo "make deploy-sh   - same, via the shell script"
	@echo "make restart     - git pull, migrate, collectstatic, restart backend + frontend (nginx untouched), notify (ENV=staging|production)"

config:
	cp config.yml.example config.yml
	@echo "config.yml replaced from config.yml.example"

install:
	pip install -r requirements.txt

deploy:
	python3 deploy.py $(ENV)

deploy-sh:
	./deploy.sh $(ENV)

restart:
	python3 restart.py $(ENV)
