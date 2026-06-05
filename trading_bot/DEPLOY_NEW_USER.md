# 🚀 New User Deployment — Single File (`bot.py`)

A brand-new user can run their **own** bot on their **own** VPS with their
**own** Binance API keys + Telegram token. Nothing is hardcoded — every key is
entered in the dashboard (Settings tab) and stored in that VPS's local
`trading_bot.db`.

You only need **one file: `bot.py`**.

---

## အဆင့် ၁ — VPS ထဲ SSH ဝင်ပါ (Login to your VPS)

```bash
ssh root@YOUR_VPS_IP
```

> `YOUR_VPS_IP` = သင့်ကိုယ်ပိုင် VPS ရဲ့ IP။ (ဒါက bot ထဲ ထည့်စရာ မဟုတ်ပါ — SSH ဝင်ဖို့ပဲ)

---

## အဆင့် ၂ — လိုအပ်တာ install + folder ဆောက်ပါ

```bash
apt update && apt install -y python3 python3-venv python3-pip build-essential libgomp1
mkdir -p /root/mybot && cd /root/mybot
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install python-binance==1.0.19 streamlit==1.35.0 pandas==2.2.0 numpy==1.26.0 \
  lightgbm==4.3.0 scikit-learn==1.4.0 joblib==1.3.2 requests==2.31.0 \
  python-telegram-bot==20.7 psutil==5.9.8 python-dotenv==1.0.0
```

> 1GB RAM VPS ဆိုရင် swap ထည့်ထားရင် ပိုလုံခြုံ:
> ```bash
> fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
> ```

---

## အဆင့် ၃ — `bot.py` ကို VPS ပေါ် တင်ပါ

`bot.py` ကို `/root/mybot/` ထဲ ထည့်ပါ။ နည်းလမ်း ၃ မျိုး:

- **scp** (ကိုယ့်စက်ကနေ): `scp bot.py root@YOUR_VPS_IP:/root/mybot/`
- **nano** (paste): `nano bot.py` → content paste → `Ctrl+O`, `Enter`, `Ctrl+X`
- **git**: `git clone <repo> && cp hpuaung/trading_bot/bot.py /root/mybot/`

---

## အဆင့် ၄ — စမ်း run ပါ

```bash
cd /root/mybot && source .venv/bin/activate
streamlit run bot.py --server.address 0.0.0.0 --server.port 8080 --server.headless true
```

Browser မှာ ဖွင့်ပါ: `http://YOUR_VPS_IP:8080`
→ **📈 Binance Futures Bot** ပေါ်ရင် အောင်မြင်ပါပြီ။ ( `Ctrl+C` နဲ့ ရပ်ပြီး အဆင့် ၅ ဆက်ပါ)

---

## အဆင့် ၅ — အမြဲ run နေအောင် (systemd service)

```bash
cat > /etc/systemd/system/mybot.service << 'EOF'
[Unit]
Description=Binance Futures Trading Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/mybot
ExecStart=/root/mybot/.venv/bin/streamlit run bot.py --server.address 0.0.0.0 --server.port 8080 --server.headless true
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload && systemctl enable mybot && systemctl start mybot
systemctl status mybot --no-pager
```

`active (running)` ပေါ်ရင် ပြီးပြီ — SSH/Termux ပိတ်လည်း bot ဆက် run နေမယ်။

---

## အဆင့် ၆ — ကိုယ်ပိုင် key တွေ ထည့်ပါ (Dashboard)

`http://YOUR_VPS_IP:8080` → **⚙️ Settings**:

1. **🔌 API Configuration** → Binance **Testnet** (သို့) **Live** API key/secret → **Test Connection** → 🟢
2. **📰 News & AI APIs** → (optional) GNews key + HuggingFace token
3. **📱 Telegram** → Bot token + Chat ID → **Test Telegram**
4. **📊 Dashboard** → pair ရွေး (max 10)
5. **⚡ Scalping / 📈 Swing** → **▶ START**

> Default က **🧪 PAPER mode** — real ပိုက်ဆံ မသုံးပါ။ Live ပြောင်းဖို့ confirmation လိုပါတယ်။
> testnet key: https://testnet.binancefuture.com (အခမဲ့)

---

## 🛠️ Service commands

| | |
|---|---|
| Log | `journalctl -u mybot -f` |
| Restart | `systemctl restart mybot` |
| Stop | `systemctl stop mybot` |

---

## ⚠️ သတိ

- user တိုင်းရဲ့ key တွေက သူ့ VPS ရဲ့ `trading_bot.db` ထဲ သီးခြား သိမ်းတယ် — **share မဖြစ်ပါ**။
- ဒါက သင့်ကိုယ်ပိုင် authorized account အတွက် trading software ပါ။ Real fund မသုံးခင် paper/testnet မှာ သေချာ စမ်းပါ။
