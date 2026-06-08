# 📱 Futures Bot — Flutter App (monitor + control)

A lightweight Flutter app that opens your bot's dashboard from the VPS and acts
as the **main monitor + control** (it shows the live Streamlit dashboard — same
balances, positions, settings, START/STOP — inside a native app shell with a
refresh button and an editable VPS address).

```
App opens ─▶ connects to your VPS (http://IP:8080) ─▶ full bot dashboard
```

---

## Build the APK

You need the Flutter SDK installed (https://docs.flutter.dev/get-started/install).

```bash
cd flutter_app
flutter pub get
flutter build apk --release
```

The installable APK is created at:
`build/app/outputs/flutter-apk/app-release.apk`

Copy it to your phone and install (allow “install from unknown sources”).

> No Flutter SDK / no PC? Use an online builder (e.g. **Codemagic**, **GitHub
> Actions with the flutter action**, or **WebIntoApp/Median.co** for a pure
> URL-to-APK wrapper) — point it at this `flutter_app` folder or your VPS URL.

---

## ⚠️ One required Android setting (cleartext HTTP)

The dashboard is served over **http** (not https), so Android needs cleartext
traffic enabled. After `flutter create`-ing the platform folders (or in the
generated project), set in
`android/app/src/main/AndroidManifest.xml` inside the `<application>` tag:

```xml
<application
    android:usesCleartextTraffic="true"
    ... >
```

and ensure the INTERNET permission is present (it is by default):

```xml
<uses-permission android:name="android.permission.INTERNET"/>
```

If your VPS later serves https, this is not needed.

---

## Using it

1. Open the app → it loads `http://150.95.84.241:8080` by default.
2. Tap the **⚙️ gear** (top-right) to change the VPS address (saved on device).
3. Tap **🔄** or pull down to refresh.
4. Log in with your dashboard password (default `admin`) and use the bottom tabs
   (Dashboard / Scalping / Swing / Settings) exactly like in the browser.

---

## Generate platform folders (first time only)

This folder ships `lib/` + `pubspec.yaml`. To get a buildable project, run once:

```bash
cd flutter_app
flutter create .          # generates android/ ios/ etc. around the existing lib/
flutter pub get
# then add usesCleartextTraffic as above, and:
flutter build apk --release
```
