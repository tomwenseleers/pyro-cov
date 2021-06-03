data:
	ln -sf ~/Google\ Drive\ File\ Stream/Shared\ drives/Pyro\ CoV data

install: FORCE
	pip install -e .[test]

lint: FORCE
	flake8
	black --check .
	isort --check .
	mypy .

format: FORCE
	black .
	isort .

test: lint data FORCE
	pytest -n auto test
	python mutrans.py --mcmc -n 2 -w 2 -s 4 -t 2 -c 1 -l 1 -f

update: FORCE
	python git_pull.py cov-lineages/lineages-website
	python git_pull.py CSSEGISandData/COVID-19
	#python git_pull.py owid/covid-19-data
	(cd ~/data/gisaid ; ./pull)
	time nice python preprocess_gisaid.py
	time python featurize_nextclade.py

ssh:
	gcloud compute ssh --project pyro-284215 --zone us-central1-c \
	  pyro-cov-fritzo-vm -- -AX

push:
	gcloud compute scp --project pyro-284215 --zone us-central1-c \
	  --recurse --compress \
	  results/gisaid.columns.pkl  \
	  pyro-cov-fritzo-vm:~/pyro-cov/results/
	gcloud compute scp --project pyro-284215 --zone us-central1-c \
	  --recurse --compress \
	  results/nextclade.features.pt \
	  pyro-cov-fritzo-vm:~/pyro-cov/results/

pull:
	gcloud compute scp --project pyro-284215 --zone us-central1-c \
	  --recurse --compress \
	  pyro-cov-fritzo-vm:~/pyro-cov/results/mutrans.pt \
	  results/

FORCE:
