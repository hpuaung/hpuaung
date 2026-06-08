import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() => runApp(const BotApp());

class BotApp extends StatelessWidget {
  const BotApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Futures Bot',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: Colors.indigo, useMaterial3: true),
      home: const HomePage(),
    );
  }
}

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  late final WebViewController _controller;
  String _url = 'http://150.95.84.241:8080';
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(Colors.white)
      ..setNavigationDelegate(NavigationDelegate(
        onPageStarted: (_) => setState(() => _loading = true),
        onPageFinished: (_) => setState(() => _loading = false),
      ));
    _loadSavedUrl();
  }

  Future<void> _loadSavedUrl() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString('vps_url') ?? _url;
    setState(() => _url = saved);
    _controller.loadRequest(Uri.parse(saved));
  }

  Future<void> _editUrl() async {
    final prefs = await SharedPreferences.getInstance();
    final ctrl = TextEditingController(text: _url);
    final result = await showDialog<String>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('VPS Address'),
        content: TextField(
          controller: ctrl,
          keyboardType: TextInputType.url,
          decoration: const InputDecoration(
            hintText: 'http://YOUR_VPS_IP:8080',
            border: OutlineInputBorder(),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(c, ctrl.text.trim()),
            child: const Text('Connect'),
          ),
        ],
      ),
    );
    if (result != null && result.isNotEmpty) {
      await prefs.setString('vps_url', result);
      setState(() => _url = result);
      _controller.loadRequest(Uri.parse(result));
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('📈 Futures Bot'),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            icon: const Icon(Icons.refresh),
            onPressed: () => _controller.reload(),
          ),
          IconButton(
            tooltip: 'VPS address',
            icon: const Icon(Icons.settings),
            onPressed: _editUrl,
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () => _controller.reload(),
        child: Stack(
          children: [
            WebViewWidget(controller: _controller),
            if (_loading) const LinearProgressIndicator(minHeight: 3),
          ],
        ),
      ),
    );
  }
}
