# Authenticate with IBM

1. Authenticate with your entitlement key. Never store credentials or keys in the repo:

```bash
docker login cp.icr.io --username cp
```

2. You should see:

```
Login Succeeded
```

If you don't see this message, check your entitlement key and try again.

# Launch HCD with Docker Compose

1. Both `compose.yaml` and `cassandra.yaml` must be present in the same `hcd-dev` folder.
2. Start the HCD node and Data API:

```bash
docker compose up -d
```

This command does the following:

- Pulls the HCD and Data API images from IBM Container Registry.
- Starts the HCD node with a 3 GB memory limit.
- Starts the Data API on port `8181` with a 2 GB memory limit.
- Creates health checks to ensure services are ready.

The initial startup might take several minutes as the images download and the database initializes.

# Verify the installation

Check that the containers are running and healthy.

View the container status:

```bash
docker compose ps
```

You should see both `hcd` and `data-api` containers with status `Up` and `healthy`.

View the logs to monitor startup progress:

```bash
docker compose logs -f
```

Press `Ctrl+C` to stop following the logs.

# Connect to HCD

After the containers are healthy, you can connect to HCD using several methods.

> **Note:** Container names include your directory name as a prefix. If you created the installation in a directory called `hcd-dev`, the containers are named `hcd-dev-hcd-1` and `hcd-dev-data-api-1`. Adjust the container names in the following commands based on your directory name.

## Use CQL shell (cqlsh)

Connect to the running container and start `cqlsh`:

```bash
docker exec -it hcd-dev-hcd-1 cqlsh -u cassandra -p cassandra
```

You should see the CQL shell prompt:

```
Connected to hcd-cluster at 192.0.2.200:9042
[cqlsh 6.1.0 | Cassandra 4.1.x | CQL spec 3.4.6 | Native protocol v5]
Use HELP for help.
```

The default credentials are username `cassandra` and password `cassandra`.

> **Warning:** When you install HCD, it creates a `cassandra` superuser role in the database. Do not use the default `cassandra` role in production — it is a security risk. Create a new superuser role for running HCD instead.

## Use the Data API and clients

The Data API is available at `http://localhost:8181`. You can use the Swagger UI to test the API at `http://localhost:8181/swagger-ui/`.

Test the connection:

```bash
curl http://localhost:8181/stargate/health
```

You can interact with the Data API over HTTP or with the clients. For more information, see Get started with the Data API (HTTP).

## Use Cassandra drivers

For CQL operations, connect your applications using standard Cassandra drivers on `localhost:9042`. Use the following connection details:

| Detail | Value |
|---|---|
| Host | `localhost` |
| Port | `9042` |
| Username | `cassandra` (or another database role) |
| Password | `cassandra` (or another database role) |
| Local datacenter | `dc1` |

For driver installation and usage, see Cassandra driver compatibility.

# View container logs

View HCD logs:

```bash
docker compose logs hcd
```

View Data API logs:

```bash
docker compose logs data-api
```

Follow logs in real-time:

```bash
docker compose logs -f
```

# Configuration details

The Docker Compose setup includes the following configuration.

| Service | Setting | Value |
|---|---|---|
| HCD service node | Image | `cp.icr.io/cp/cpd/ibm-datastax-hcd/hcd:2.0.8-ubi` |
| HCD service node | Memory limit | 3 GB |
| HCD service node | Max heap size | 1536 MB |
| HCD service node | Cluster name | `hcd-cluster` |
| HCD service node | CQL port | `9042` |
| HCD service node | Platform | `linux/amd64` |
| Data API service | Image | `cp.icr.io/cp/cpd/ibm-datastax-hcd/data-api:v1.0.53` |
| Data API service | Memory limit | 2 GB |
| Data API service | HTTP port | `8181` |
| Data API service | Contact points | `hcd` ¹ |
| Data API service | Local datacenter | `dc1` |
| Data API service | Features | Document API, Table API, Vectorize |

¹ This value must match the name of the HCD service defined in `compose.yaml`.

# Customize the configuration

You can modify the `compose.yaml` and `cassandra.yaml` files to customize your installation.

## Change memory limits

Edit `compose.yaml` to adjust memory limits:

```yaml
services:
  hcd:
    mem_limit: 4G  # Increase HCD memory
    environment:
      - MAX_HEAP_SIZE=2048M  # Increase heap size
```

## Change ports

Edit `compose.yaml` to use different ports:

```yaml
services:
  hcd:
    ports:
      - 9043:9042  # Use port 9043 instead of 9042
  data-api:
    ports:
      - 8182:8181  # Use port 8182 instead of 8181
```

## Modify settings

Edit `cassandra.yaml` to change database settings, such as memory allocations and global compaction behavior.

# Troubleshoot issues

Use these troubleshooting tips to resolve common issues. Contact IBM Support if you continue to experience issues.

## Authentication fails

If you can't authenticate with the ICR:

- Verify your entitlement key is valid.
- Ensure you're using `cp` as the username.
- Check that you have access to the HCD container images.

## Containers fail to start

Check the container logs:

```bash
docker compose logs
```

Common issues:

- **Insufficient memory:** Increase Docker's memory allocation to at least 6 GB total.
- **Port conflicts:** Ensure ports `9042` and `8181` are available.
- **Disk space:** Verify sufficient disk space is available (at least 10 GB free).
- **Image pull errors:** Verify authentication with IBM Container Registry.
- **Platform mismatch:** The images are built for `linux/amd64`. On Apple Silicon Macs, Docker/Podman uses emulation.

## Can't connect to CQL

Verify the HCD container is running and healthy:

```bash
docker compose ps
docker exec -it hcd-dev-hcd-1 nodetool status
```

Wait for the health check to pass. The initial startup can take a few minutes.

## Data API can't connect to HCD

Ensure the HCD container is healthy before the Data API starts.

1. Check the HCD container status:

```bash
docker compose ps hcd
```

You should see status `Up` and `healthy`.

2. If the HCD container is not healthy, check its logs:

```bash
docker compose logs hcd
```

Look for startup errors or initialization issues.

3. Verify the HCD node is ready:

```bash
docker exec -it hcd-dev-hcd-1 nodetool status
```

You should see status `UN` (Up/Normal).

4. If the Data API started before HCD was ready, restart it:

```bash
docker compose restart data-api
```

The Docker Compose configuration includes a health check dependency, but in some cases you might need to manually restart the Data API.

5. Verify the Data API can reach HCD:

```bash
docker compose logs data-api
```

Look for successful connection messages to the contact point.

## Performance issues on Apple Silicon

If you're running on Apple Silicon (M1/M2/M3) Macs:

- The `linux/amd64` images run under emulation, which impacts performance.
- Increase memory allocation in Docker Desktop settings.

## Container names

The actual container names include the directory name as a prefix. If you created the installation in a directory called `hcd-dev`, the containers are named:

- `hcd-dev-hcd-1`
- `hcd-dev-data-api-1`

Adjust commands accordingly based on your directory name.