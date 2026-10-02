// 元器件物料管理系统 —— 便携版启动器(免安装)
//
// 编译(csc 随 .NET Framework 自带,无需安装任何东西):
//   csc /nologo /target:winexe /r:System.Windows.Forms.dll /win32icon:app.ico \
//       /out:元器件物料管理.exe Launcher.cs
//
// 生命周期不依赖浏览器进程,而是靠前端心跳:
//   runtime\python.exe app\server.py --idle-exit 120   → 网页每 5 秒打一次 /api/ping
//   窗口一关 → 请求停了 → 服务空闲超时自杀 → 启动器发现服务没了 → 一起退出
// 这样 Edge 能不能用、开的是不是 --app 窗口,都不影响收尾。
using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("元器件物料管理系统")]
[assembly: AssemblyProduct("元器件物料管理系统")]
[assembly: AssemblyDescription("本地免安装的元器件库存 / 出入库 / 项目 BOM 管理工具")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

internal static class Launcher
{
    private const int FirstPort = 8000;
    private const int PortTries = 200;
    private const int ReadyTimeoutMs = 20000;
    private const int IdleExitSeconds = 120;   // 与服务端心跳超时保持一致

    private static readonly string Root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
    private static readonly object LogLock = new object();
    private static Process _server;

    private static string PythonPath { get { return Path.Combine(Root, @"runtime\python.exe"); } }
    private static string ServerPath { get { return Path.Combine(Root, @"app\server.py"); } }
    private static string DataDir { get { return Path.Combine(Root, "data"); } }
    private static string LogPath { get { return Path.Combine(DataDir, "server.log"); } }

    [STAThread]
    private static int Main()
    {
        try
        {
            if (!File.Exists(PythonPath))
                return Fail("缺少内嵌运行时:\n" + PythonPath +
                            "\n\n请确认整个文件夹是完整解压出来的(不要只复制 exe)。");
            if (!File.Exists(ServerPath))
                return Fail("缺少程序文件:\n" + ServerPath);

            Directory.CreateDirectory(DataDir);
            AppendLog("──────── 启动 " + Root);

            // 已经有「本副本」的服务在跑就直接复用,避免开出第二个进程抢同一个数据库
            bool reuse = IsOurServer(FirstPort);
            int port = reuse ? FirstPort : FirstFreePort(FirstPort);
            if (port <= 0)
                return Fail("本机 " + FirstPort + "-" + (FirstPort + PortTries - 1) +
                            " 端口都被占用了,请先关掉一些程序再试。");

            if (reuse)
            {
                AppendLog("复用已在运行的服务,端口 " + port);
            }
            else
            {
                AppendLog("启动服务,端口 " + port + ",空闲超时 " + IdleExitSeconds + " 秒");
                if (!StartServer(port))
                    return Fail("无法启动服务进程。\n\n最近日志:\n" + TailLog(15));

                if (!WaitReady(port))
                {
                    StopServer();
                    return Fail("服务在 " + (ReadyTimeoutMs / 1000) + " 秒内没能就绪。" +
                                "\n\n最近日志:\n" + TailLog(15));
                }
                AppendLog("服务已就绪");
            }

            OpenWindow(port);

            if (!reuse)
            {
                AppendLog("等待界面关闭(前端心跳停止后服务会自行退出)…");
                while (IsOurServer(port)) Thread.Sleep(3000);
                AppendLog("服务已退出,启动器结束");
            }
            return 0;
        }
        catch (Exception ex)
        {
            return Fail("启动器出错:\n" + ex.GetType().Name + ": " + ex.Message);
        }
        finally
        {
            StopServer();
        }
    }

    // ---------------------------------------------------------- 服务进程

    private static bool StartServer(int port)
    {
        ProcessStartInfo psi = new ProcessStartInfo(
            PythonPath,
            "\"" + ServerPath + "\" --host 127.0.0.1 --port " + port +
            " --idle-exit " + IdleExitSeconds);
        psi.WorkingDirectory = Root;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        // 强制 UTF-8,日志里中文才不会变成问号
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";

        _server = new Process();
        _server.StartInfo = psi;
        _server.OutputDataReceived += delegate(object s, DataReceivedEventArgs e)
        {
            if (e.Data != null) AppendLog("  " + e.Data);
        };
        _server.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e)
        {
            if (e.Data != null) AppendLog("  " + e.Data);
        };

        try
        {
            if (!_server.Start()) return false;
            _server.BeginOutputReadLine();
            _server.BeginErrorReadLine();
            return true;
        }
        catch (Exception ex)
        {
            AppendLog("启动失败: " + ex.Message);
            return false;
        }
    }

    private static bool WaitReady(int port)
    {
        int waited = 0;
        while (waited < ReadyTimeoutMs)
        {
            if (_server != null && _server.HasExited)
            {
                AppendLog("服务进程提前退出,退出码 " + _server.ExitCode);
                return false;
            }
            if (IsOurServer(port)) return true;
            Thread.Sleep(150);
            waited += 150;
        }
        return false;
    }

    private static void StopServer()
    {
        if (_server == null) return;
        try
        {
            if (!_server.HasExited)
            {
                _server.Kill();
                _server.WaitForExit(5000);
                AppendLog("服务已停止");
            }
        }
        catch { }
        try { _server.Dispose(); } catch { }
        _server = null;
    }

    /// <summary>
    /// 探测该端口上是不是「本程序的这一份副本」。
    /// 只认 /api/health,并且要求 root 完全一致 —— 否则会把别的副本(或别的程序)
    /// 误当成自己,导致界面显示的是另一份数据。
    /// </summary>
    private static bool IsOurServer(int port)
    {
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(
                "http://127.0.0.1:" + port + "/api/health");
            req.Timeout = 1500;
            req.Proxy = null;              // 关键:本机回环不能走系统代理
            req.Method = "GET";
            req.KeepAlive = false;
            using (WebResponse resp = req.GetResponse())
            using (Stream s = resp.GetResponseStream())
            using (StreamReader sr = new StreamReader(s, Encoding.UTF8))
            {
                string body = sr.ReadToEnd();
                if (body.IndexOf("\"parts-manager\"", StringComparison.Ordinal) < 0) return false;
                // JSON 里的反斜杠是转义的,比较前要把本地路径也转义成同样形式
                string escapedRoot = Root.Replace("\\", "\\\\");
                return body.IndexOf(escapedRoot, StringComparison.OrdinalIgnoreCase) >= 0;
            }
        }
        catch
        {
            return false;
        }
    }

    private static int FirstFreePort(int start)
    {
        for (int p = start; p < start + PortTries; p++)
        {
            TcpListener listener = null;
            try
            {
                listener = new TcpListener(IPAddress.Loopback, p);
                listener.Start();
                listener.Stop();
                return p;
            }
            catch
            {
                if (listener != null) { try { listener.Stop(); } catch { } }
            }
        }
        return 0;
    }

    // ---------------------------------------------------------- 窗口

    private static void OpenWindow(int port)
    {
        string url = "http://127.0.0.1:" + port + "/";
        int pingsBefore = GetPings(port);
        string edge = FindEdge();

        if (edge != null)
        {
            // 配置目录放在程序自己的文件夹里,而不是 %LOCALAPPDATA%:
            //   * 完全自带,整个文件夹拷到 U 盘也能用
            //   * 不依赖用户配置目录的写权限
            string profile = Path.Combine(Root, "profile");
            try { Directory.CreateDirectory(profile); } catch { }

            string args = "--app=" + url
                        + " --user-data-dir=\"" + profile + "\""
                        + " --no-first-run --no-default-browser-check --noerrdialogs"
                        + " --disable-background-mode"
                        + " --disable-features=Translate,msEdgeFirstRunExperience"
                        + " --window-size=1400,920";

            if (Launch(edge, args, url))
            {
                // 关键:Edge 进程活着不代表窗口显示出来了(进程可能秒退、也可能被环境挡住)。
                // 真正可信的信号是页面发来的心跳。
                AppendLog("等待界面加载(以页面心跳为准)…");
                for (int i = 0; i < 24; i++)      // 最多约 12 秒
                {
                    if (GetPings(port) > pingsBefore)
                    {
                        AppendLog("界面已加载,心跳正常");
                        return;
                    }
                    Thread.Sleep(500);
                }
                AppendLog("Edge 拉起来了但页面一直没有心跳 —— 窗口多半没显示出来,改用默认浏览器");
            }
        }
        else
        {
            AppendLog("没找到 Edge,改用默认浏览器");
        }

        Fallback(url);
    }

    private static bool Launch(string exe, string args, string url)
    {
        try
        {
            AppendLog("用 Edge 打开无边框应用窗口: " + url);
            Process p = Process.Start(new ProcessStartInfo(exe, args) { UseShellExecute = false });
            // 不跟随该进程:不少 Edge 版本会「交接」给已有进程后立刻退出,
            // 拿它判断生命周期会误杀服务。一切以心跳为准。
            AppendLog(p == null ? "Edge 启动返回空" : "Edge 已拉起 PID=" + p.Id);
            return p != null;
        }
        catch (Exception ex)
        {
            AppendLog("Edge 启动失败: " + ex.Message);
            return false;
        }
    }

    /// <summary>读取服务端累计的页面心跳次数;拿不到返回 -1。</summary>
    private static int GetPings(int port)
    {
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(
                "http://127.0.0.1:" + port + "/api/health");
            req.Timeout = 1500;
            req.Proxy = null;
            req.KeepAlive = false;
            using (WebResponse resp = req.GetResponse())
            using (Stream s = resp.GetResponseStream())
            using (StreamReader sr = new StreamReader(s, Encoding.UTF8))
            {
                string body = sr.ReadToEnd();
                int i = body.IndexOf("\"pings\"", StringComparison.Ordinal);
                if (i < 0) return -1;
                i = body.IndexOf(':', i);
                if (i < 0) return -1;
                i++;
                while (i < body.Length && !char.IsDigit(body[i])) i++;
                int j = i;
                while (j < body.Length && char.IsDigit(body[j])) j++;
                if (j == i) return -1;
                return int.Parse(body.Substring(i, j - i));
            }
        }
        catch
        {
            return -1;
        }
    }

    /// <summary>Edge 不可用时的退路:交给默认浏览器,同样靠心跳收尾。</summary>
    private static void Fallback(string url)
    {
        try
        {
            AppendLog("用默认浏览器打开 " + url);
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception ex) { AppendLog("连默认浏览器也打不开: " + ex.Message); }
    }

    private static string FindEdge()
    {
        string[] candidates = new string[]
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                         @"Microsoft\Edge\Application\msedge.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                         @"Microsoft\Edge\Application\msedge.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                         @"Microsoft\Edge\Application\msedge.exe"),
        };
        foreach (string c in candidates)
            if (File.Exists(c)) return c;
        return null;
    }

    // ---------------------------------------------------------- 日志

    private static void AppendLog(string line)
    {
        lock (LogLock)
        {
            try
            {
                FileInfo fi = new FileInfo(LogPath);
                if (fi.Exists && fi.Length > 4L * 1024 * 1024) File.Delete(LogPath);
                using (StreamWriter sw = new StreamWriter(LogPath, true, new UTF8Encoding(false)))
                    sw.WriteLine(DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + line);
            }
            catch { }
        }
    }

    private static string TailLog(int n)
    {
        try
        {
            if (!File.Exists(LogPath)) return "(还没有日志)";
            string[] lines = File.ReadAllLines(LogPath, Encoding.UTF8);
            int from = Math.Max(0, lines.Length - n);
            StringBuilder sb = new StringBuilder();
            for (int i = from; i < lines.Length; i++) sb.AppendLine(lines[i]);
            string s = sb.ToString().Trim();
            return s.Length == 0 ? "(日志为空)" : s;
        }
        catch
        {
            return "(日志读不出来)";
        }
    }

    private static int Fail(string message)
    {
        try { AppendLog("[错误] " + message.Replace("\n", " ")); }
        catch { }
        MessageBox.Show(message, "元器件物料管理系统",
                        MessageBoxButtons.OK, MessageBoxIcon.Warning);
        return 1;
    }
}
