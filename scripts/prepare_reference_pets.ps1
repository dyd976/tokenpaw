$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies @([System.Drawing.Bitmap].Assembly.Location) -TypeDefinition @"
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Collections.Generic;

public static class ReferencePetCropper
{
    private static bool IsBackground(Color color)
    {
        int min = Math.Min(color.R, Math.Min(color.G, color.B));
        int max = Math.Max(color.R, Math.Max(color.G, color.B));
        return min >= 226 && max - min <= 28;
    }

    public static void Crop(string sourcePath, string outputPath, int x, int y, int width, int height)
    {
        using (var source = new Bitmap(sourcePath))
        using (var crop = new Bitmap(width, height, PixelFormat.Format32bppArgb))
        {
        using (var graphics = Graphics.FromImage(crop))
        {
            graphics.Clear(Color.Transparent);
            graphics.DrawImage(source, new Rectangle(0, 0, width, height), new Rectangle(x, y, width, height), GraphicsUnit.Pixel);
        }

        var remove = new bool[width, height];
        var queue = new Queue<Point>();
        Action<int, int> enqueue = (px, py) =>
        {
            if (px < 0 || py < 0 || px >= width || py >= height || remove[px, py] || !IsBackground(crop.GetPixel(px, py))) return;
            remove[px, py] = true;
            queue.Enqueue(new Point(px, py));
        };
        for (int px = 0; px < width; px++) { enqueue(px, 0); enqueue(px, height - 1); }
        for (int py = 0; py < height; py++) { enqueue(0, py); enqueue(width - 1, py); }
        while (queue.Count > 0)
        {
            var point = queue.Dequeue();
            enqueue(point.X - 1, point.Y); enqueue(point.X + 1, point.Y);
            enqueue(point.X, point.Y - 1); enqueue(point.X, point.Y + 1);
        }
        for (int px = 0; px < width; px++) for (int py = 0; py < height; py++)
            if (remove[px, py]) crop.SetPixel(px, py, Color.Transparent);

        int minX = width, minY = height, maxX = -1, maxY = -1;
        for (int px = 0; px < width; px++) for (int py = 0; py < height; py++)
            if (crop.GetPixel(px, py).A > 0) { minX = Math.Min(minX, px); minY = Math.Min(minY, py); maxX = Math.Max(maxX, px); maxY = Math.Max(maxY, py); }
        if (maxX < 0) throw new InvalidOperationException("No foreground pixels found in crop.");

        int pad = 10;
        minX = Math.Max(0, minX - pad); minY = Math.Max(0, minY - pad);
        maxX = Math.Min(width - 1, maxX + pad); maxY = Math.Min(height - 1, maxY + pad);
        using (var trimmed = crop.Clone(new Rectangle(minX, minY, maxX - minX + 1, maxY - minY + 1), PixelFormat.Format32bppArgb))
        using (var canvas = new Bitmap(512, 512, PixelFormat.Format32bppArgb))
        {
        using (var graphics = Graphics.FromImage(canvas))
        {
            graphics.Clear(Color.Transparent);
            graphics.InterpolationMode = InterpolationMode.NearestNeighbor;
            graphics.PixelOffsetMode = PixelOffsetMode.Half;
            float scale = Math.Min(480f / trimmed.Width, 480f / trimmed.Height);
            int drawWidth = Math.Max(1, (int)(trimmed.Width * scale));
            int drawHeight = Math.Max(1, (int)(trimmed.Height * scale));
            int drawX = (512 - drawWidth) / 2;
            int drawY = 512 - drawHeight - 12;
            graphics.DrawImage(trimmed, new Rectangle(drawX, drawY, drawWidth, drawHeight));
        }
        canvas.Save(outputPath, ImageFormat.Png);
        }
        }
        }
    }
"@

$root = Split-Path -Parent $PSScriptRoot
$source = Get-ChildItem -LiteralPath (Join-Path $root "Resource") -File | Where-Object { $_.Extension.ToLowerInvariant() -eq ".png" } | Select-Object -First 1 -ExpandProperty FullName
if (-not $source) { throw "No PNG reference image found in Resource." }
$output = Join-Path $root "desktop\src\assets\characters\reference"
$null = New-Item -ItemType Directory -Force -Path $output

$crops = @(
    @{ Name = "gemini-reference.png"; X = 0; Y = 145; Width = 365; Height = 445 },
    @{ Name = "gpt-reference.png"; X = 365; Y = 145; Width = 370; Height = 445 },
    @{ Name = "claude-reference.png"; X = 735; Y = 145; Width = 350; Height = 445 },
    @{ Name = "grok-reference.png"; X = 1085; Y = 145; Width = 363; Height = 445 }
)

foreach ($crop in $crops) {
    $destination = Join-Path $output $crop.Name
    [ReferencePetCropper]::Crop($source, $destination, $crop.X, $crop.Y, $crop.Width, $crop.Height)
    Write-Output "Created $destination"
}
