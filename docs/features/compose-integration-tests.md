# Offline Compose lifecycle tests

The Compose runner tests the installed Robert wheel using real daemon, scanner,
publisher, Worker, supervisor, Web, SQLite, and Git processes. A fixture service
supplies the GitHub CLI responses and stores simulated remote effects separately.
Scripted model processes use the same input and result protocols as configured
workers.

Run from the repository root with Python 3.11 or later, Git, and a Docker engine
with Compose v2. The first build needs access to image and package registries.
The running services use an internal network and isolated named volumes.

```sh
python3 -B tests/simulations/compose/run.py --list
python3 -B tests/simulations/compose/run.py --suite smoke
python3 -B tests/simulations/compose/run.py --scenario P02 --artifacts /tmp/robert-publication
python3 -B tests/simulations/compose/run.py --suite core --shard-count 4 --shard-index 0
python3 -B tests/simulations/compose/run.py --suite extended
python3 -B tests/simulations/compose/run.py --suite slow
```

The inventory printed by `--list` is the set of implemented cases. A family
selector such as `P02` runs each implemented case within that family. Use
`--repeat` for independent runs with fresh projects and volumes. Use an empty
artifact directory per run.

The runner writes JSON and JUnit results, Compose logs, and a bounded evidence
archive per case. The archive includes a SQLite backup, remote effects and
requests, actual Git refs, process identities, and the fault timeline. When
diagnosing a lost reply, correlate the fixture effect checkpoint with the old
publisher process group and the new publisher's receipt in the database. An
unknown fixture command or an unmatched fault makes the case fail.

By default the runner removes its containers and volumes. To investigate a local
failure, use `--keep-on-failure` and run the cleanup command it prints when
finished. The runner never mounts the host's GitHub configuration, agent
credentials, repository checkout, or Docker socket into the services.

The suite covers the Linux process and filesystem behavior exercised by its
scenarios. Real GitHub and model-provider compatibility, production credentials,
and native macOS service behavior need their own acceptance tests.
