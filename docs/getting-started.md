# Try RCAnnon

Start a local demo and see how RCAnnon explains a Kubernetes incident. You need Git, Bash, and Docker with Compose. On Windows, use a WSL terminal with Docker Desktop integration.

## Try the web app

```sh
git clone https://github.com/jkuzel02/RCAnnon.git
cd RCAnnon
bash try-rcannon.sh
```

The launcher starts the published image and prints the address, demo login, and sample request.

1. Open the printed address, normally [http://127.0.0.1:3081](http://127.0.0.1:3081).
2. Sign in with the printed demo email and password.
3. Select **rcannon-optimal** and paste the complete request printed by the launcher.

The report explains why the sample container's configured memory limit is too low for its workload. The demo replays a recorded answer so you can try the browser workflow without a Kubernetes cluster, AI account, or configuration file.

Stop the demo with `bash try-rcannon.sh down`. Run the launcher again if you want a new sample request. If another app uses port 3081, start with `RCANNON_DEMO_PORT=3082 bash try-rcannon.sh`.

## What do you want to do next?

- [Run the RCAnnon image with an existing LibreChat](container-image.md#run-the-api-with-your-own-librechat)
- [Run the complete stack against your own Kubernetes cluster](deploy-compose.md)
- [Try an example case with your own model](../tests/prompt_checks/README.md)
