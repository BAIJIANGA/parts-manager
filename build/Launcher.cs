// 元器件物料管理系统 —— 便携版启动器(免安装 / 纯本地 / 无浏览器)
//
// 编译(csc 随 .NET Framework 自带,无需安装任何东西):
//   csc /nologo /target:winexe /r:System.Windows.Forms.dll /win32icon:app.ico \
//       /out:元器件物料管理.exe Launcher.cs
//
// 它只做三件事:
//   1. 用内嵌的 runtime\pythonw.exe 拉起 app\gui.py —— 原生 Tkinter 窗口
//   2. 全程把界面进程的输出写进 data\gui.log(出问题时这就是唯一的线索)
//   3. 界面关掉后自己退出
//
// 这里【没有】HTTP 服务、没有端口、没有浏览器 —— 整个程序就是一个本地窗口,
// 完全不碰网络。
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("元器件物料管理系统")]
[assembly: AssemblyProduct("元器件物料管理系统")]
[assembly: AssemblyDescription("本地免安装的元器件库存 / 出入库 / 项目 BOM 管理工具(桌面版)")]
[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]

internal static class Launcher
{
    // 同一时刻只允许一个副本,避免两个窗口改同一个数据库
    private const string MutexName = "Global\\PartsManagerDesktop_SingleInstance";
    private const int LogKeepBytes = 4 * 1024 * 1024;

    private static readonly string Root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
    private static readonly object LogLock = new object();

    private static string PythonwPath { get { return Path.Combine(Root, @"runtime\pythonw.exe"); } }
    private static string PythonPath { get { return Path.Combine(Root, @"runtime\python.exe"); } }
    private static string GuiPath { get { return Path.Combine(Root, @"app\gui.py"); } }
    private static string DataDir { get { return Path.Combine(Root, "data"); } }
    private static string DbPath { get { return Path.Combine(DataDir, "parts.db"); } }
    private static string LogPath { get { return Path.Combine(DataDir, "gui.log"); } }

    [STAThread]
    private static int Main()
    {
        Mutex single = null;
        try
        {
            if (!File.Exists(PythonwPath) && !File.Exists(PythonPath))
                return Fail("缺少内嵌运行时:\n" + PythonwPath +
                            "\n\n请确认整个文件夹是完整解压出来的(不要只把 exe 拷走)。");
            if (!File.Exists(GuiPath))
                return Fail("缺少程序文件:\n" + GuiPath);

            try
            {
                bool created;
                single = new Mutex(true, MutexName, out created);
                if (!created)
                {
                    MessageBox.Show("程序已经在运行了。\n\n请到任务栏找那个窗口。",
                                    "元器件物料管理系统", MessageBoxButtons.OK,
                                    MessageBoxIcon.Information);
                    return 0;
                }
            }
            catch (Exception ex)
            {
                // 拿不到互斥体不该拦住使用
                AppendLog("单实例检查跳过:" + ex.Message);
                single = null;
            }

            Directory.CreateDirectory(DataDir);
            AppendLog("──────── 启动桌面版 " + Root);

            if (File.Exists(PythonwPath))
                return RunGui(PythonwPath);

            // 没有 pythonw 就只有 python.exe —— 会多一个控制台窗口,但功能一样
            AppendLog("没有 pythonw.exe,改用 python.exe(会出现一个控制台窗口)");
            return RunGui(PythonPath);
        }
        catch (Exception ex)
        {
            return Fail("启动器出错:\n" + ex.GetType().Name + ": " + ex.Message);
        }
        finally
        {
            if (single != null)
            {
                try { single.ReleaseMutex(); } catch { }
                try { single.Dispose(); } catch { }
            }
        }
    }

    private static int RunGui(string python)
    {
        ProcessStartInfo psi = new ProcessStartInfo(python,
            "\"" + GuiPath + "\" \"" + DbPath + "\"");
        psi.WorkingDirectory = Root;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        // 强制 UTF-8,日志里的中文才不会是问号
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
        // 别往程序目录里写 .pyc —— 便携包放在 U 盘/只读介质上也不会报错,
        // 目录始终是发出去时的样子
        psi.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";

        Process gui = new Process();
        gui.StartInfo = psi;
        gui.OutputDataReceived += delegate(object s, DataReceivedEventArgs e)
        {
            if (e.Data != null) AppendLog("  " + e.Data);
        };
        gui.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e)
        {
            if (e.Data != null) AppendLog("  " + e.Data);
        };

        Stopwatch clock = Stopwatch.StartNew();
        try
        {
            if (!gui.Start())
                return Fail("无法启动界面进程。\n\n最近日志:\n" + TailLog(20));
            gui.BeginOutputReadLine();
            gui.BeginErrorReadLine();

            AppendLog("界面进程 PID=" + gui.Id + " 已启动,等待窗口关闭…");
            gui.WaitForExit();
            clock.Stop();
            AppendLog("界面已关闭,退出码 " + gui.ExitCode +
                      ",运行 " + Math.Round(clock.Elapsed.TotalSeconds) + " 秒");
        }
        catch (Exception ex)
        {
            AppendLog("界面进程异常:" + ex.Message);
            return Fail("界面进程出错:\n" + ex.Message + "\n\n最近日志:\n" + TailLog(20));
        }

        // 只有「短时间内以正数退出码结束」才算启动就崩。
        //   * 负退出码 = 被外部强杀(任务管理器 / 关机),不是崩溃
        //   * 跑够久了才退出 = 用户自己关的窗口,哪怕退出码非 0 也别吓唬人
        bool startupCrash = gui.ExitCode > 0 && clock.Elapsed.TotalSeconds < 8;
        if (startupCrash)
            return Fail("界面启动失败(退出码 " + gui.ExitCode + ")。\n\n最近日志:\n" + TailLog(20));

        return 0;
    }

    // ---------------------------------------------------------- 日志

    private static void AppendLog(string line)
    {
        lock (LogLock)
        {
            try
            {
                FileInfo fi = new FileInfo(LogPath);
                if (fi.Exists && fi.Length > LogKeepBytes)
                {
                    string old = LogPath + ".1";
                    try { File.Delete(old); } catch { }
                    File.Move(LogPath, old);
                }
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
