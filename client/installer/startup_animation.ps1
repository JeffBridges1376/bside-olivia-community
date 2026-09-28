param(
    [Parameter(Mandatory = $true)][string]$MediaPath,
    [string]$ReportPath,
    [int]$ClientProcessId = 0
)

$ErrorActionPreference = 'Stop'
$script:report = [ordered]@{ status = 'starting'; loaded = $false; ended = $false; error = $null; geometry = $null }
$script:exitCode = 0
$script:window = $null
$script:media = $null

function Save-Report {
    if ($ReportPath) {
        try {
            $script:report.updated_at = [DateTimeOffset]::UtcNow.ToString('o')
            $script:report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
        } catch { } # Optional diagnostics must not prevent playback or closing.
    }
}

function Fail-Media([string]$Message) {
    $script:exitCode = 1
    $script:report.status = 'error'
    $script:report.error = $Message
    Save-Report
    if ($script:window) { $script:window.Close() }
}

try {
    if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
        throw 'Run this script with powershell.exe -STA.'
    }
    $file = Get-Item -LiteralPath $MediaPath -ErrorAction Stop
    if ($file.PSIsContainer -or $file.Length -eq 0) { throw 'MediaPath must be a non-empty local media file.' }
    Add-Type -AssemblyName PresentationFramework
    Add-Type -ReferencedAssemblies System.Drawing @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Drawing;
using System.Collections.Generic;
public static class OliviaStartupReady {
  public delegate bool Callback(IntPtr h, IntPtr p);
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int L,T,R,B; }
  [DllImport("user32.dll")] static extern bool EnumWindows(Callback cb, IntPtr p);
  [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, Callback cb, IntPtr p);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] static extern bool IsHungAppWindow(IntPtr h);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out Rect r);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder name, int size);
  [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  static bool Painted(IntPtr h, Rect r) {
    try {
      using(var bitmap=new Bitmap(r.R-r.L,r.B-r.T)) using(var graphics=Graphics.FromImage(bitmap)) {
        IntPtr dc=graphics.GetHdc(); bool painted;
        try { painted=PrintWindow(h,dc,2); } finally { graphics.ReleaseHdc(dc); }
        if(!painted) return false;
        var colors=new HashSet<int>();
        for(int y=80;y<bitmap.Height-20;y+=17) for(int x=30;x<bitmap.Width-20;x+=17) colors.Add(bitmap.GetPixel(x,y).ToArgb());
        return colors.Count>=16; // Blank loading surfaces cannot hand off the overlay.
      }
    } catch { return false; }
  }
  public static long Find(int pid) {
    long found=0;
    EnumWindows((h,p)=> {
      uint owner; Rect r; GetWindowThreadProcessId(h,out owner); GetWindowRect(h,out r);
      if(owner!=pid || !IsWindowVisible(h) || IsIconic(h) || IsHungAppWindow(h) || r.R-r.L<600 || r.B-r.T<400) return true;
      EnumChildWindows(h,(c,q)=> {
        Rect cr; GetWindowRect(c,out cr); var name=new StringBuilder(256); GetClassName(c,name,256);
        string cls=name.ToString();
        if(IsWindowVisible(c) && cr.R-cr.L>=(r.R-r.L)*0.8 && cr.B-cr.T>=(r.B-r.T)*0.8 &&
           (cls.StartsWith("Chrome_RenderWidgetHostHWND") || (cls.StartsWith("Qt") && cls.EndsWith("QWindowIcon"))) && Painted(c,cr)) found=h.ToInt64();
        return found==0;
      },IntPtr.Zero);
      return found==0;
    },IntPtr.Zero);
    return found;
  }
}
'@

    # WPF WorkArea and Window coordinates are device-independent pixels (DIP).
    # Use the primary work area, excluding the taskbar, without mixing physical pixels.
    $work = [Windows.SystemParameters]::WorkArea
    $width = [Math]::Sqrt($work.Width * $work.Height / 6 * 16 / 9)
    $height = $width * 9 / 16
    $script:window = New-Object Windows.Window
    $window.Title = 'Community - Awakening Edition'
    $window.WindowStyle = 'None'
    $window.ResizeMode = 'NoResize'
    $window.WindowStartupLocation = 'Manual'
    $window.Width = $width
    $window.Height = $height
    $window.Left = $work.Left + ($work.Width - $width) / 2
    $window.Top = $work.Top + ($work.Height - $height) / 2
    $window.Background = [Windows.Media.Brushes]::Black
    $window.ShowInTaskbar = $true
    $window.Topmost = $true

    $script:media = New-Object Windows.Controls.MediaElement
    $media.LoadedBehavior = 'Manual'
    $media.UnloadedBehavior = 'Close'
    $media.Stretch = 'Uniform'
    $media.Volume = 1.0
    $media.IsMuted = $false
    $window.Content = $media

    $window.Add_Loaded({
        try {
            $scale = [Windows.PresentationSource]::FromVisual($window).CompositionTarget.TransformToDevice
            $script:report.geometry = [ordered]@{
                left = $window.Left; top = $window.Top
                width = $window.ActualWidth; height = $window.ActualHeight
                requested_width = $width; requested_height = $height
                work_left = $work.Left; work_top = $work.Top
                work_width = $work.Width; work_height = $work.Height
                dpi_scale_x = $scale.M11; dpi_scale_y = $scale.M22
            }
            $script:report.status = 'window_loaded'
            Save-Report
            $media.Source = [Uri]$file.FullName
            $media.Play()
        } catch { Fail-Media $_.Exception.Message }
    })
    $media.Add_MediaOpened({
        if (-not $media.HasVideo -or -not $media.NaturalDuration.HasTimeSpan -or $media.NaturalDuration.TimeSpan.TotalSeconds -le 0) {
            Fail-Media 'Media must contain a playable video with a finite duration.'
            return
        }
        $script:report.loaded = $true
        $script:report.status = 'loaded'
        $script:report.duration_seconds = $media.NaturalDuration.TimeSpan.TotalSeconds
        $script:report.has_audio = $media.HasAudio
        $script:report.video_width = $media.NaturalVideoWidth
        $script:report.video_height = $media.NaturalVideoHeight
        Save-Report
    })
    $media.Add_MediaEnded({
        $media.Pause() # Hold the final decoded frame; never loop the audio.
        $script:report.ended = $true
        $script:report.status = 'holding_last_frame'
        Save-Report
        if ($ClientProcessId -le 0) { $window.Close() }
    })
    $media.Add_MediaFailed({ param($sender, $eventArgs) Fail-Media $eventArgs.ErrorException.Message })
    $window.Add_PreviewKeyDown({
        param($sender, $eventArgs)
        if ($eventArgs.Key -eq [Windows.Input.Key]::Escape) {
            $eventArgs.Handled = $true
            $script:report.status = 'skipped'
            Save-Report
            $window.Close()
        }
    })
    $window.Add_Closed({
        $media.Close()
        if ($script:timer) { $script:timer.Stop() }
        if ($script:report.status -notin @('holding_last_frame', 'ready', 'client_exited', 'skipped', 'error')) {
            $script:report.status = 'closed'
        }
        Save-Report
    })
    $script:readyCount = 0
    $script:timer = New-Object Windows.Threading.DispatcherTimer
    $timer.Interval = [TimeSpan]::FromMilliseconds(500)
    $timer.Add_Tick({
        if ($ClientProcessId -le 0) { return }
        if (-not (Get-Process -Id $ClientProcessId -ErrorAction SilentlyContinue)) {
            $script:report.status = 'client_exited'; Save-Report; $window.Close(); return
        }
        if (-not $script:report.ended) { return }
        $handle = [OliviaStartupReady]::Find($ClientProcessId)
        if ($handle -gt 0) { $script:readyCount++ } else { $script:readyCount = 0 }
        if ($script:report.ended -and $script:readyCount -ge 3) {
            $script:report.status = 'ready'; $script:report.main_window = $handle
            Save-Report; $window.Close()
            $null = [OliviaStartupReady]::SetForegroundWindow([IntPtr]$handle)
            return
        }
    })
    $timer.Start()
    $null = $window.ShowDialog()
} catch {
    Fail-Media $_.Exception.Message
} finally {
    if ($script:media) { $script:media.Close() }
}
exit $script:exitCode
