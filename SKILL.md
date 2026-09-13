---
name: nis_radar
description: Nişindeki YouTube kanallarını (video + Shorts) her gün otomatik tarayıp transkriptlerinden Türkçe özet raporu çıkaran kişisel radar sistemini kurar, çalıştırır ve yönetir. "niche radar", "youtube kanal takibi", "rakipleri izle", "günlük video özeti", "transkript raporu" gibi isteklerde kullan.
---

# Niş Radar

Kullanıcının kendi bilgisayarında çalışan günlük YouTube izleme asistanı. Akış:
`yt-dlp ile keşif (videos + shorts sekmesi)` → `altyazı indir (video indirmeden)` → `claude -p ile özet` → `~/NicheRadar/reports/YYYY-MM-DD.md` → `bildirim`.

Tüm mantık `scripts/radar.py` içindedir (sadece Python standart kütüphanesi). Sen kod YAZMAZSIN; script'i kurar, çalıştırır ve çıktıyı kullanıcıya anlatırsın.

## Değişmez kurallar

1. **Cloud'da çalıştırma.** YouTube, AWS/GCP/Azure IP'lerinden altyazı isteğini engeller. Bu sistem kullanıcının kendi makinesinde (Mac/Windows/Linux masaüstü) çalışmalıdır. Sunucuya/VPS'e taşıma önerme.
2. **Kullanıcının kendi YouTube/Instagram hesabı kullanılmaz.** Cookie ile giriş yalnızca kullanıcı açıkça isterse ve ban riskini söyleyerek (`ytdlp_extra_args`).
3. **Script'i yeniden yazma.** Hata varsa önce `doctor` ve `logs/radar.log`. Değişiklik gerekiyorsa `config.json` üzerinden.
4. **Kurulum adımlarını tek tek doğrula.** Her komutun çıktısını oku; "muhtemelen çalıştı" yok.

## Kurulum akışı (ilk kez)

Bu skill'in klasörü: `SKILL_DIR` (bu dosyanın bulunduğu klasör). Script: `SKILL_DIR/scripts/radar.py`.

### 0. Ortamı tespit et
- İşletim sistemi (`uname` / `ver`), Python (`python3 --version`, Windows'ta `python --version`), `claude --version`, `uv --version`, `yt-dlp --version`.
- Python 3.9+ gerekir. macOS'ta hazır gelir. Windows'ta yoksa: `uv python install 3.12` ve script'i `uv run --python 3.12 python radar.py ...` ile çalıştır.
- `claude` yoksa dur: Claude Code kurulu ve giriş yapılmış olmalı (özetleme `claude -p` ile yapılır, kullanıcının aboneliğini kullanır).

### 1. Bağımlılıklar
- `uv` yoksa: macOS/Linux `curl -LsSf https://astral.sh/uv/install.sh | sh`, Windows PowerShell `irm https://astral.sh/uv/install.ps1 | iex`. Kurulumdan sonra yeni PATH'i yükle (`source ~/.zshrc` veya terminali yeniden aç).
- `uv tool install "yt-dlp[default,curl-cffi]"` (curl-cffi bot kontrolü riskini azaltır).
- ffmpeg gerekmez. Sadece altyazısı olmayan videolar için Whisper yedeği istenirse gerekir (varsayılan kapalı).

### Windows notu (tüm komutlar için geçerli)
`python3` yerine `python` (Python yoksa `uv run --python 3.12 python`), `~/NicheRadar` yerine `%USERPROFILE%\NicheRadar` (PowerShell'de `$HOME\NicheRadar`). Aşağıdaki komutlar Unix biçiminde yazıldı; Windows'ta bu dönüşümü uygula.

### 2. Kur
```
python3 SKILL_DIR/scripts/radar.py install
```
`~/NicheRadar/` altında `config.json`, `prompt.md`, `digest_prompt.md`, `radar.py` kopyası, `reports/`, `logs/`, `cache/` oluşur. Bundan sonra komutları `~/NicheRadar/radar.py` üzerinden çalıştır.

### 3. Kanalları al
Önce kapsamı söyle: "Bu sistem şu an sadece YouTube'u takip ediyor (video + Shorts). Instagram, TikTok, LinkedIn yok."
Sonra sor: "Aklında takip etmek istediğin kanallar var mı? Varsa linklerini yapıştır. Yoksa nişini / işini söyle, senin için kanal önereyim."

**A) Kullanıcı kanal verdiyse:** `@handle`, kanal URL'i veya `UC...` id kabul edilir; video linki değil kanal linki. Hepsini tek komutta ekle.

**B) Kullanıcı niş söylediyse:** iki soru daha sor: "Global (İngilizce) kanallar mı, Türkiye'deki (Türkçe) kanallar mı, ikisi de mi?" ve "Rakiplerin mi (senin işini yapanlar), yoksa senin müşterinin izlediği üreticiler mi?" Sonra web araması yap (ör. "best <niş> youtube channels 2026", "<niş> youtube kanalları"), 5-8 aday çıkar: kanal adı, @handle, tek cümle neden (abone/sıklık/konu). Her adayı `add-channel` ile ÇÖZÜMLEYEREK doğrula; çözülmeyeni listeden at, uydurma handle önerme. Kullanıcı seçsin, seçilenleri ekle. 3-6 kanal ideal; 10'dan fazlasını önerme (günlük tavan 20 içerik).

Ekleme komutu (iki yol için de aynı):
```
python3 ~/NicheRadar/radar.py add-channel @nicksaraev @NateHerk
```
Çözümleme yt-dlp ile yapılır; hata alırsan handle'ı YouTube'da doğrula. Eklenen listeyi `list` ile göster ve onaylat.

### 4. Kurulum soruları (hepsini SOR, cevapları `~/NicheRadar/config.json`'a yaz)
Kanallar eklendikten sonra şu üç soruyu sırayla, kısa ve Türkçe sor; cevap gelmeden varsayım yapma:
1. **Geçmiş:** "Başlangıçta geçmişe ne kadar bakalım? Son 7 gün / son 30 gün / hiç (sadece bundan sonra çıkanlar)."
   - 7 gün → `first_run_days: 7`, `first_run_items: 10`
   - 30 gün → `first_run_days: 30`, `first_run_items: 30` (ilk gün en fazla `max_per_run` kadarı işlenir, kalanı sonraki günlere kayar; kullanıcıya söyle)
   - hiç → `first_run_days: 0`, `first_run_items: 0` (ilk çalışma mevcut içeriği "görüldü" sayar, hiçbir şey işlemez)
2. **Saat:** "Rapor her gün saat kaçta gelsin?" → `schedule_time` (HH:MM, 00-23:00-59; script geçersiz değeri reddeder). Varsayılan önerin 08:00.
3. **Dil:** "Özetler hangi dilde olsun?" → `summary_lang` (varsayılan "Türkçe").

Sormadan varsayılan bırak: `report_dir` (boşsa `~/NicheRadar/reports`; Obsidian gibi bir klasör isterse tam yol), `telegram` (isteğe bağlı; BotFather token + @userinfobot chat id), `model` ("sonnet" ucuz ve yeterli), `max_per_run` (günlük tavan 20), `max_age_days` (14 günden eski içerik günlük çalışmada atlanır).

### 5. Sağlık kontrolü
```
python3 ~/NicheRadar/radar.py doctor
```
"SONUC: hazir" görmeden ilerleme.

### 6. İlk çalışma
Önce keşfi göster, sonra gerçek çalıştır:
```
python3 ~/NicheRadar/radar.py run --dry-run
python3 ~/NicheRadar/radar.py run --limit 3
```
İlk çalışma, kullanıcının seçtiği geçmiş penceresindeki (`first_run_days`: 7 / 30 / 0) tüm video ve Shorts içeriklerini işler (kanal+sekme başına en fazla `first_run_items`); daha eskiler "görüldü" sayılır. 0 seçildiyse ilk çalışma hiçbir şey işlemez, sadece başlangıç noktasını koyar. Sonraki çalışmalar yalnızca yeni yüklemeleri işler.
Raporu (`reports/YYYY-MM-DD.md`) aç, kullanıcıya ilk 20-30 satırı göster.

### 7. Zamanlayıcı
```
python3 ~/NicheRadar/radar.py schedule install
python3 ~/NicheRadar/radar.py schedule status
```
- macOS: `~/Library/LaunchAgents/com.nicheradar.daily.plist`. Uykudan uyanınca kaçan çalışmayı yapar; Mac kapalıysa yapmaz. Anında test: `launchctl kickstart -k gui/$(id -u)/com.nicheradar.daily` sonra `logs/launchd.out.log`.
- Windows: Task Scheduler görevi "NicheRadar", WakeToRun + StartWhenAvailable açık. Anında test: `schtasks /Run /TN NicheRadar`.
- Linux: cron satırı ekrana yazılır.

### 8. Kullanıcıya teslim
Şunları açıkça söyle: rapor nerede, kanal nasıl eklenir (`add-channel`), nasıl kapatılır (`schedule remove`), hata olursa ne yapılır (`doctor` + `logs/radar.log`), günlük maliyet (Claude aboneliği içinde, ekstra altyapı yok).

## Sorun giderme

| Belirti | Sebep | Çözüm |
|---|---|---|
| "Sign in to confirm you're not a bot" | VPN, Private Relay, CGNAT veya yoğun istek | VPN/Relay kapat; `sleep_seconds` artır; son çare `ytdlp_extra_args: ["--cookies-from-browser","chrome"]` (hesap riski, kullanıcıya söyle) |
| O gün rapor dosyası oluşmadı, logda "yeni video yok" | Gerçekten yeni içerik yok | Normal, rapor sadece yeni içerik varsa yazılır. `state.json` içindeki seen listesine bak |
| "transkript yok" | Kanal altyazıyı kapatmış veya auto-caption henüz oluşmamış | Yarın tekrar denemek için `state.json`'dan id'yi sil; ya da `whisper.enabled: true` + ffmpeg |
| Claude hatası / zaman aşımı | `claude` login düşmüş veya launchd PATH'i eksik | Terminalde `claude -p "ok"` dene; `schedule install` PATH'i yeniden yazar |
| Kanal çözülemedi | Handle yanlış | YouTube'da kanal sayfasını aç, URL'deki `@handle`'ı kullan |
| yt-dlp bozuldu | YouTube değişikliği | `uv tool upgrade yt-dlp` |

## Paylaşım
Bu klasörü olduğu gibi `~/.claude/skills/nis_radar/` (Windows: `%USERPROFILE%\.claude\skills\nis_radar\`) altına kopyalayan herkes Claude Code'da `/nis_radar` yazıp aynı kurulumu yapar.
