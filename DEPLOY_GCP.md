# Deploying to GCP — maximum optimized (minimal credit, full performance)

Goal: run the bot for **~$0** by living inside GCP's **Always Free** tier, and
eliminate the only real cost (egress) so your $300 credit barely moves.

## The cost model (read this first)

| Resource | Optimized choice | Cost |
| --- | --- | --- |
| Compute (VM) | `e2-micro` in a free-tier region | **$0** (Always Free) |
| Disk | 30 GB standard persistent disk | **$0** (Always Free) |
| Egress (user downloads) | **Serve files from Cloudflare R2** | **~$0** (R2 has zero egress) |
| Egress (if NOT using R2) | Standard network tier | ~$0.085/GB after 1 GB free |

**The single most important optimization: enable R2** so downloads don't leave
the VM. Without it, every GB users download costs ~$0.085-0.12 and will drain the
credit. With R2, the VM only does cheap one-time uploads and the bot runs
essentially free, even at high traffic.

---

## 1. Create the free VM

GCP Console → Compute Engine → **Create Instance**:

- **Region:** `us-central1`, `us-west1`, or `us-east1` (Always Free only works here)
- **Machine type:** `e2-micro` (Always Free eligible)
- **Boot disk:** Debian 12, **Standard persistent disk, 30 GB** (not SSD — SSD costs more)
- **Networking → Network Service Tier:** **Standard** (cheaper egress than Premium)
- **Firewall:** check **Allow HTTP traffic**
- Create.

Note the VM's **External IP** (you'll use it for `BASE_URL`).

> Performance note: `e2-micro` is shared-core with ~1 GB RAM, but this bot is
> I/O-bound (streaming + `-c copy` merges, no transcoding), so it performs great.
> Network bandwidth on GCP scales with the VM and is plenty for downloads.

## 2. Open the web ports (80, and 443 if you use a domain)

VPC network → Firewall → **Create firewall rule**:
- Name: `allow-web`
- Targets: All instances (or a tag)
- Source IPv4 ranges: `0.0.0.0/0`
- Protocols/ports: TCP `80,443`
- Create.

(Caddy listens on 80/443 and proxies to the bot internally, so you don't need
to expose 8080 publicly.)

## 3. SSH in and prepare the VM

Click **SSH** on the VM, then:

```bash
# Install Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# Add 2 GB swap so the 1 GB RAM never OOMs during merges
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Get the code and configure

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/ajmalyaseen/file-to-link.git
cd file-to-link

# Create .env (copy the example and edit)
cp .env.example .env
nano .env
```

Set at least:
```
API_ID=7429385
API_HASH=2082adce3c41697ef60081760ffb80eb
BOT_TOKEN=your-token
SECRET_KEY=44e70dbbe365c3dca9220543912f85d32556ba4f2d7bd5e46ef7d17b04e83da9
LOG_CHANNEL_ID=-100...           # your private storage channel
BASE_URL=https://alaskafiletolink.duckdns.org   # DuckDNS domain (Download button works)
EXPIRY_SECONDS=86400
```

> **Why DuckDNS, not sslip.io?** Telegram rejects "Download Now" URL buttons for
> hostnames that embed an IP (sslip.io does). A DuckDNS domain has no embedded
> IP, so the button works. Register a free subdomain at https://www.duckdns.org,
> point it to `136.110.36.47`, and match the name in `Caddyfile` + `BASE_URL`.

For near-zero egress (highly recommended), also set:
```
R2_ENABLED=true
R2_SINGLE_FILES=true
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=...
```

## 5. Run it

```bash
docker compose up -d --build
docker compose logs -f        # watch startup; Ctrl+C to stop watching
```

It auto-restarts on crash and on VM reboot.

## 6. Test

Open the bot in Telegram → `/start` → send a file → you get an HTTPS link at
`https://alaska.136.110.36.47.sslip.io/stream/...` that downloads anywhere.

---

## Keep credit usage minimal — checklist

- ✅ **e2-micro in a free region** → compute free
- ✅ **30 GB standard disk** → disk free
- ✅ **Standard network tier** → cheaper egress
- ✅ **R2 for downloads** → egress ~free (the big one)
- ✅ **Set a budget alert:** Billing → Budgets & alerts → budget at $5 and $50,
  email alerts. So you can never be surprised.
- ✅ **EXPIRY_SECONDS=86400** → files don't linger and pile up.

## Updating later

```bash
cd file-to-link
git pull
docker compose up -d --build
```

## URLs & HTTPS (Caddy + sslip.io — already set up)

`docker compose` runs **Caddy**, which serves your bot over **free HTTPS** using
sslip.io — no domain to buy. Links look like:
`https://alaska.136.110.36.47.sslip.io/...`

- The `Caddyfile` is preset; just keep ports **80 and 443** open (step 2) — port
  80 is needed for the certificate challenge.
- Change the `alaska` prefix in both the `Caddyfile` and `BASE_URL` if you want.

Prefer your own/DuckDNS domain? Point its A record to the VM IP, swap the
hostname in the `Caddyfile`, and set `BASE_URL=https://yourdomain`.
