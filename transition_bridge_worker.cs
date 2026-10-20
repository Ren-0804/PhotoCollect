using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

[assembly: System.Reflection.AssemblyTitle("PhotoCollect Transition Worker")]
[assembly: System.Reflection.AssemblyProduct("PhotoCollect")]
[assembly: System.Reflection.AssemblyFileVersion("__FILE_VERSION__")]

internal static class Program
{
    private const string ProductVersion = "__VERSION__";
    private const long PayloadSize = __PAYLOAD_SIZE__L;
    private const string PayloadSha256 = "__PAYLOAD_SHA256__";
    private const int HealthTimeoutSeconds = __HEALTH_TIMEOUT_SECONDS__;
    private const string InstallMutexName = "Local\\PhotoCollectSelfUpdate";

    private sealed class State
    {
        internal string TargetPath;
        internal string PayloadPath;
        internal string OriginalBackupPath;
        internal string OriginalHash;
        internal string BridgeBackupPath;
        internal string FinalHealthPath;
        internal string FinalToken;
        internal int BootstrapPid;
        internal long BootstrapStartTicks;
        internal string BridgeHash;
        internal string WorkerPath;
        internal string LogPath;
        internal string HandoffPath;
        internal string UpdateRoot;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        State state = null;
        try
        {
            if (args.Length != 2 || args[0] != "--bridge-worker")
                return 64;
            string statePath = Path.GetFullPath(args[1]);
            state = ReadState(statePath);
            ValidateState(state, statePath);
            RunTransition(state, statePath);
            return 0;
        }
        catch (Exception error)
        {
            if (state != null)
            {
                Log(state.LogPath, "Transition failed: " + error.Message);
            }
            else
            {
                Log(DefaultLogPath(), "Worker rejected transition state: " + error.Message);
            }
            return 1;
        }
    }

    private static void RunTransition(State state, string statePath)
    {
        Mutex installMutex = new Mutex(false, InstallMutexName);
        bool ownsMutex = false;
        Process started = null;
        bool committed = false;
        try
        {
            try
            {
                ownsMutex = installMutex.WaitOne(120000);
            }
            catch (AbandonedMutexException)
            {
                ownsMutex = true;
            }
            if (!ownsMutex)
                throw new TimeoutException("Timed out waiting for the original updater.");

            AssertFile(state.TargetPath, -1, state.BridgeHash, "transition bridge");
            AssertFile(
                state.OriginalBackupPath,
                -1,
                state.OriginalHash,
                "previous PhotoCollect version");
            AssertFile(state.PayloadPath, PayloadSize, PayloadSha256, "real payload");
            WriteAtomicText(state.HandoffPath, "ready");
            Log(state.LogPath, "Original updater completed; bridge handoff accepted.");

            WaitForBootstrapExit(state);
            AssertFile(state.TargetPath, -1, state.BridgeHash, "transition bridge");
            AssertFile(state.PayloadPath, PayloadSize, PayloadSha256, "real payload");
            DeleteIfExists(state.FinalHealthPath);
            DeleteIfExists(state.BridgeBackupPath);
            ReplaceWithRetry(
                state.PayloadPath,
                state.TargetPath,
                state.BridgeBackupPath,
                30);
            committed = true;
            Log(state.LogPath, "Real PhotoCollect executable committed atomically.");

            started = StartPhotoCollect(
                state.TargetPath,
                state.FinalHealthPath,
                state.FinalToken);
            Log(
                state.LogPath,
                "Started real PhotoCollect v" + ProductVersion + " as PID "
                    + started.Id.ToString(CultureInfo.InvariantCulture) + ".");
            if (!WaitForHealth(state, started, HealthTimeoutSeconds))
                throw new InvalidOperationException(
                    "Real PhotoCollect did not report healthy startup.");

            Log(state.LogPath, "Transition succeeded.");
            DeleteIfExists(state.FinalHealthPath);
            DeleteIfExists(state.BridgeBackupPath);
            DeleteIfExists(state.HandoffPath);
            DeleteIfExists(statePath);
        }
        catch
        {
            if (started != null)
                StopProcess(started);
            if (committed || File.Exists(state.TargetPath))
                RestorePreviousVersion(state);
            throw;
        }
        finally
        {
            if (started != null)
                started.Dispose();
            if (ownsMutex)
            {
                try { installMutex.ReleaseMutex(); }
                catch { }
            }
            installMutex.Dispose();
        }
    }

    private static void ValidateState(State state, string statePath)
    {
        string worker = Path.GetFullPath(
            Process.GetCurrentProcess().MainModule.FileName);
        if (!String.Equals(worker, state.WorkerPath, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Worker path does not match transition state.");
        if (!String.Equals(state.TargetPath + ".previous", state.OriginalBackupPath,
            StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Previous-version backup path is invalid.");
        if (!String.Equals(state.TargetPath + ".bridge", state.BridgeBackupPath,
            StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Bridge backup path is invalid.");
        RequireChild(state.UpdateRoot, state.PayloadPath);
        RequireChild(state.UpdateRoot, state.WorkerPath);
        RequireChild(state.UpdateRoot, state.FinalHealthPath);
        RequireChild(state.UpdateRoot, state.LogPath);
        RequireChild(state.UpdateRoot, state.HandoffPath);
        RequireChild(state.UpdateRoot, statePath);
        if (!Regex.IsMatch(state.FinalToken, "^[0-9a-f]{32}$"))
            throw new InvalidDataException("Final health token is invalid.");
        if (!String.Equals(
            Path.GetFileName(state.FinalHealthPath),
            "health-" + state.FinalToken + ".json",
            StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Final health marker path is invalid.");
        if (state.BootstrapPid <= 0 || state.BootstrapStartTicks <= 0)
            throw new InvalidDataException("Bootstrap process identity is invalid.");
        if (!Regex.IsMatch(state.BridgeHash, "^[0-9a-f]{64}$") ||
            !Regex.IsMatch(state.OriginalHash, "^[0-9a-f]{64}$"))
            throw new InvalidDataException("Transition file hash is invalid.");
    }

    private static State ReadState(string path)
    {
        string[] encoded = File.ReadAllLines(path, Encoding.UTF8);
        if (encoded.Length != 16)
            throw new InvalidDataException("Transition state is incomplete.");
        string[] values = new string[encoded.Length];
        for (int index = 0; index < encoded.Length; index++)
            values[index] = Encoding.UTF8.GetString(Convert.FromBase64String(encoded[index]));
        if (values[0] != "1" || values[1] != ProductVersion)
            throw new InvalidDataException("Transition state version is invalid.");
        return new State
        {
            TargetPath = Path.GetFullPath(values[2]),
            PayloadPath = Path.GetFullPath(values[3]),
            OriginalBackupPath = Path.GetFullPath(values[4]),
            OriginalHash = values[5],
            BridgeBackupPath = Path.GetFullPath(values[6]),
            FinalHealthPath = Path.GetFullPath(values[7]),
            FinalToken = values[8],
            BootstrapPid = Int32.Parse(values[9], CultureInfo.InvariantCulture),
            BootstrapStartTicks = Int64.Parse(values[10], CultureInfo.InvariantCulture),
            BridgeHash = values[11],
            WorkerPath = Path.GetFullPath(values[12]),
            LogPath = Path.GetFullPath(values[13]),
            HandoffPath = Path.GetFullPath(values[14]),
            UpdateRoot = Path.GetFullPath(values[15])
        };
    }

    private static void RequireChild(string root, string candidate)
    {
        string expected = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        string actualParent = Path.GetDirectoryName(Path.GetFullPath(candidate));
        if (!String.Equals(expected, actualParent, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Transition artifact escaped the update directory.");
    }

    private static void WaitForBootstrapExit(State state)
    {
        Process bootstrap;
        try
        {
            bootstrap = Process.GetProcessById(state.BootstrapPid);
        }
        catch (ArgumentException)
        {
            return;
        }
        using (bootstrap)
        {
            long actualTicks;
            try { actualTicks = bootstrap.StartTime.ToUniversalTime().Ticks; }
            catch (InvalidOperationException) { return; }
            if (actualTicks != state.BootstrapStartTicks)
                return;
            if (!bootstrap.WaitForExit(60000))
                throw new TimeoutException("Transition bridge did not exit in time.");
        }
    }

    private static Process StartPhotoCollect(
        string target,
        string healthPath,
        string token)
    {
        ProcessStartInfo start = new ProcessStartInfo();
        start.FileName = target;
        start.WorkingDirectory = Path.GetDirectoryName(target);
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.Arguments = "--post-update-health " + Quote(healthPath)
            + " --post-update-token " + token;
        start.EnvironmentVariables["PYINSTALLER_RESET_ENVIRONMENT"] = "1";
        Process process = Process.Start(start);
        if (process == null)
            throw new InvalidOperationException("Unable to start real PhotoCollect.");
        return process;
    }

    private static bool WaitForHealth(State state, Process process, int seconds)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < deadline)
        {
            if (File.Exists(state.FinalHealthPath))
            {
                try
                {
                    string json = File.ReadAllText(state.FinalHealthPath, Encoding.UTF8);
                    Match token = Regex.Match(json, "\\\"token\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"");
                    Match version = Regex.Match(json, "\\\"version\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"");
                    Match pid = Regex.Match(json, "\\\"pid\\\"\\s*:\\s*([0-9]+)");
                    int markerPid;
                    if (token.Success && version.Success && pid.Success &&
                        token.Groups[1].Value == state.FinalToken &&
                        version.Groups[1].Value == ProductVersion &&
                        Int32.TryParse(pid.Groups[1].Value, out markerPid) &&
                        IsExpectedHealthProcess(
                            state.TargetPath,
                            markerPid,
                            process))
                        return true;
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            if (process.HasExited)
                return false;
            Thread.Sleep(250);
        }
        return false;
    }

    private static bool IsExpectedHealthProcess(
        string targetPath,
        int markerPid,
        Process launchedProcess)
    {
        if (markerPid <= 0)
            return false;
        if (markerPid == launchedProcess.Id)
        {
            try { return !launchedProcess.HasExited; }
            catch { return false; }
        }
        try
        {
            using (Process markerProcess = Process.GetProcessById(markerPid))
            {
                if (markerProcess.HasExited)
                    return false;
                string actualPath = Path.GetFullPath(markerProcess.MainModule.FileName);
                return String.Equals(
                    actualPath,
                    Path.GetFullPath(targetPath),
                    StringComparison.OrdinalIgnoreCase);
            }
        }
        catch
        {
            return false;
        }
    }

    private static void RestorePreviousVersion(State state)
    {
        if (!File.Exists(state.OriginalBackupPath))
            throw new FileNotFoundException(
                "Previous PhotoCollect backup is missing.",
                state.OriginalBackupPath);
        AssertFile(
            state.OriginalBackupPath,
            -1,
            state.OriginalHash,
            "previous PhotoCollect version");
        if (File.Exists(state.TargetPath))
        {
            string currentHash = Sha256File(state.TargetPath);
            if (!String.Equals(currentHash, state.BridgeHash,
                    StringComparison.OrdinalIgnoreCase) &&
                !String.Equals(currentHash, PayloadSha256,
                    StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException(
                    "Installed executable changed during transition; rollback was stopped.");
            string failed = state.TargetPath + ".transition-failed";
            DeleteIfExists(failed);
            ReplaceWithRetry(
                state.OriginalBackupPath,
                state.TargetPath,
                failed,
                30);
            DeleteIfExists(failed);
        }
        else
        {
            File.Move(state.OriginalBackupPath, state.TargetPath);
        }
        StartWithoutHealth(state.TargetPath);
        Log(state.LogPath, "Rollback completed and previous version restarted.");
    }

    private static void StartWithoutHealth(string target)
    {
        ProcessStartInfo start = new ProcessStartInfo();
        start.FileName = target;
        start.WorkingDirectory = Path.GetDirectoryName(target);
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.EnvironmentVariables["PYINSTALLER_RESET_ENVIRONMENT"] = "1";
        Process.Start(start);
    }

    private static void StopProcess(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill();
                process.WaitForExit(10000);
            }
        }
        catch { }
    }

    private static void AssertFile(
        string path,
        long expectedSize,
        string expectedHash,
        string label)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException(label + " is missing.", path);
        FileInfo item = new FileInfo(path);
        if (expectedSize >= 0 && item.Length != expectedSize)
            throw new InvalidDataException(label + " size mismatch.");
        if (!String.Equals(
            Sha256File(path),
            expectedHash,
            StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException(label + " SHA256 mismatch.");
    }

    private static void ReplaceWithRetry(
        string source,
        string destination,
        string backup,
        int timeoutSeconds)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        Exception lastError = null;
        while (DateTime.UtcNow < deadline)
        {
            if (!File.Exists(source))
                throw new FileNotFoundException("Atomic replacement source is missing.", source);
            if (!File.Exists(destination))
                throw new FileNotFoundException(
                    "Atomic replacement destination is missing.",
                    destination);
            try
            {
                File.Replace(source, destination, backup, true);
                return;
            }
            catch (IOException error)
            {
                lastError = error;
            }
            catch (UnauthorizedAccessException error)
            {
                lastError = error;
            }
            Thread.Sleep(250);
        }
        throw new IOException(
            "Atomic replacement remained locked after "
                + timeoutSeconds.ToString(CultureInfo.InvariantCulture)
                + " seconds.",
            lastError);
    }

    private static string Sha256File(string path)
    {
        using (SHA256 algorithm = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
        {
            return BitConverter.ToString(algorithm.ComputeHash(stream))
                .Replace("-", "").ToLowerInvariant();
        }
    }

    private static void WriteAtomicText(string path, string content)
    {
        string temporary = path + ".tmp." + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(temporary, content, new UTF8Encoding(false));
            if (File.Exists(path)) File.Replace(temporary, path, null, true);
            else File.Move(temporary, path);
        }
        finally
        {
            DeleteIfExists(temporary);
        }
    }

    private static string Quote(string value)
    {
        if (value.IndexOf('"') >= 0)
            throw new InvalidDataException("Transition path contains a quote.");
        return "\"" + value + "\"";
    }

    private static void Log(string path, string message)
    {
        try
        {
            File.AppendAllText(
                path,
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)
                    + " " + message + Environment.NewLine,
                new UTF8Encoding(false));
        }
        catch { }
    }

    private static string DefaultLogPath()
    {
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (String.IsNullOrWhiteSpace(local))
            local = Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData);
        return Path.Combine(
            local,
            "PhotoCollect",
            "updates",
            "transition-v" + ProductVersion + ".log");
    }

    private static void DeleteIfExists(string path)
    {
        try
        {
            if (File.Exists(path)) File.Delete(path);
        }
        catch { }
    }
}
