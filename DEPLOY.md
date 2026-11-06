# Deploying the chatbot on EC2 with Docker

AWS credentials are **never baked into the image**. They are supplied at run time.

## 1. Get the image onto the instance

**ECR (recommended)**
```bash
aws ecr create-repository --repository-name silknet-chatbot
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com
docker tag silknet-chatbot:latest <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/silknet-chatbot:latest
docker push <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/silknet-chatbot:latest
# on the EC2 instance: log in the same way, then docker pull
```

**Without ECR**
```bash
docker save silknet-chatbot:latest | gzip > chatbot.tar.gz   # copy with scp
docker load < chatbot.tar.gz                                  # on the instance
```

## 2. Provide AWS credentials (pick one)

### Option A — IAM instance role (recommended, nothing to copy)
Attach a role to the instance with `bedrock:InvokeModel` (and `bedrock:InvokeModelWithResponseStream`)
permission on the model. boto3 finds the role credentials automatically; they rotate on their own.

Containers need the instance metadata hop limit raised to 2, otherwise boto3 cannot reach it:
```bash
aws ec2 modify-instance-metadata-options --instance-id <ID> \
  --http-tokens required --http-put-response-hop-limit 2
```
Run:
```bash
docker run -d --name chatbot --restart unless-stopped -p 5000:5000 \
  -e AWS_REGION=us-east-1 -v chatbot-data:/data silknet-chatbot:latest
```

### Option B — reuse the AWS CLI config on the instance (`~/.aws`)
Run `aws configure` on the instance (or copy your `credentials` and `config` files there with
`scp`, then `chmod 600 ~/.aws/*`), then mount it read-only:
```bash
docker run -d --name chatbot --restart unless-stopped -p 5000:5000 \
  -e AWS_REGION=us-east-1 \
  -e AWS_SHARED_CREDENTIALS_FILE=/aws/credentials \
  -e AWS_CONFIG_FILE=/aws/config \
  -v ~/.aws:/aws:ro -v chatbot-data:/data silknet-chatbot:latest
```
Add `-e AWS_PROFILE=<name>` for a non-default profile. The container runs as UID 1000, which is
`ec2-user` on Amazon Linux and `ubuntu` on Ubuntu, so the 600-permission files are readable.
Does not work for SSO profiles (token refresh needs write access, and tokens expire).

### Option C — environment variables
```bash
docker run ... -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... [-e AWS_SESSION_TOKEN=...] ...
```
Least preferred: values show up in `docker inspect` and shell history.

## 3. Compose
`docker-compose.yml` implements Option B. For Option A, delete the `${HOME}/.aws` volume line and the
two `AWS_*_FILE` variables, then `docker compose up -d --build`.

## 4. Networking and data
- Open inbound TCP **5000** in the instance security group, restricted to your IP or VPN. The app has
  **no login**, so don't expose it publicly.
- Conversation history lives in the `chatbot-data` volume (`/data/chat_history.db`).
  Back it up with: `docker run --rm -v chatbot-data:/data -v $PWD:/b alpine cp /data/chat_history.db /b/`
- Logs: `docker logs -f chatbot`. Health: `docker ps` shows `(healthy)`.
