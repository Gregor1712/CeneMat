#:package Azure.AI.DocumentIntelligence@1.0.0
#:package Azure.Identity@1.21.0
#:package PdfPig@0.1.16
#:package Tomlyn@2.10.1
// Spúšťa sa cez dotnet run, nepublikuje sa ako NativeAOT; vypne varovania o reflexii pri AOT
#:property PublishAot=false
// Strany sa vyrezávajú cez PdfPig. PDFsharp 6.5 je pod AGPL-3.0 a bez licenčného kľúča odmietne otvoriť PDF.

// Experiment: prevod PDF cenníka cez Azure AI Document Intelligence (model prebuilt-layout).
// C# podoba experiments/azure_di_skuska.py: rovnaký konfiguračný súbor azure_di.toml a rovnaký tvar výstupu.
//
// Spustenie z koreňa projektu (.NET 10 SDK, súbor sa spúšťa priamo, bez .csproj):
//     dotnet run experiments/azure_di_skuska.cs -- Cenniky/Prefa_Sucany/Prefa_kamenivo_2026.pdf
//     dotnet run experiments/azure_di_skuska.cs -- <pdf> --strany 3-6 --profil standard
//     dotnet run experiments/azure_di_skuska.cs -- <pdf> --strany 20 --z-json
//
// Výstup ide do experiments/vystup/<názov PDF>-azure-cs/, aby sa dal porovnať s Python verziou
// (experiments/porovnanie.py):
//     dokument.md         celý obsah ako Markdown, ako ho vráti služba
//     vysledok.json       kompletné odpovede služby po dávkach, rovnaký formát ako v Pythone
//     tabulka-NN.csv      každá tabuľka zvlášť, zlúčené bunky sú zopakované v každom políčku

using System.ClientModel.Primitives;
using System.Diagnostics;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using Azure;
using Azure.AI.DocumentIntelligence;
using Azure.Identity;
using Tomlyn;
using Tomlyn.Model;
using UglyToad.PdfPig;
using UglyToad.PdfPig.Writer;

Console.OutputEncoding = Encoding.UTF8;
try
{
    await RunAsync(args);
    return 0;
}
catch (UsageException exception)
{
    Console.Error.WriteLine(exception.Message);
    return 1;
}

static async Task RunAsync(string[] args)
{
    string experimentsDir = SourceDirectory();
    Arguments arguments = Arguments.Parse(args, Path.Combine(experimentsDir, "azure_di.toml"));
    (string profileName, Profile profile) = Profile.Load(arguments.ConfigPath, arguments.ProfileName);

    int pageCount;
    using (PdfDocument source = PdfDocument.Open(arguments.Pdf))
    {
        pageCount = source.NumberOfPages;
    }

    List<int> pages = ParsePages(arguments.Pages, pageCount);
    string outDir = Path.Combine(experimentsDir, "vystup", Path.GetFileNameWithoutExtension(arguments.Pdf) + "-azure-cs");
    Directory.CreateDirectory(outDir);
    string jsonPath = Path.Combine(outDir, "vysledok.json");

    Console.WriteLine($"Súbor: {arguments.Pdf}");
    if (arguments.FromJson)
    {
        Console.WriteLine("Režim: z uloženého vysledok.json, Azure sa nevolá");
    }
    else
    {
        double cost = pages.Count * profile.PricePer1000Eur / 1000;
        Console.WriteLine($"Profil: {profileName} ({profile.Description})");
        Console.WriteLine($"Strany: {pages[0]}–{pages[^1]} ({pages.Count}), odhad ceny: {cost:0.000} €");
    }

    Stopwatch stopwatch = Stopwatch.StartNew();
    List<AnalyzedPart> parts;
    if (arguments.FromJson)
    {
        parts = LoadSaved(jsonPath, pages);
    }
    else
    {
        parts = await AnalyzeAsync(arguments.Pdf, pages, profile);
        await File.WriteAllTextAsync(
            Path.Combine(outDir, "dokument.md"), string.Join("\n\n", parts.Select(part => part.Result.Content ?? "")));
        await File.WriteAllTextAsync(jsonPath, SaveJson(profileName, parts));
    }

    stopwatch.Stop();
    foreach (string old in Directory.GetFiles(outDir, "tabulka-*.csv"))
    {
        File.Delete(old);
    }

    List<(List<int> Pages, DocumentTable Table)> tables = parts
        .SelectMany(part => part.Result.Tables.Select(table => (part.Pages, table)))
        .ToList();
    int paragraphs = parts.Sum(part => part.Result.Paragraphs.Count);
    Console.WriteLine($"Čas: {stopwatch.Elapsed.TotalSeconds:0.0} s, odseky: {paragraphs}, tabuľky: {tables.Count}");

    for (int index = 0; index < tables.Count; index++)
    {
        (List<int> chunk, DocumentTable table) = tables[index];
        (string[] header, List<string[]> body) = TableToRows(table);
        WriteCsv(Path.Combine(outDir, $"tabulka-{index + 1:00}.csv"), header, body);

        // Služba čísluje strany vyrezaného PDF od 1, preložíme ich späť na strany pôvodného cenníka.
        int regionPage = table.BoundingRegions.Count > 0 ? table.BoundingRegions[0].PageNumber : 0;
        string page = regionPage >= 1 && regionPage <= chunk.Count ? chunk[regionPage - 1].ToString() : "?";
        Console.WriteLine();
        Console.WriteLine($"=== Tabuľka {index + 1} (strana {page}), {body.Count} riadkov × {header.Length} stĺpcov");
        Console.WriteLine("Stĺpce: " + string.Join(" | ", header));
        foreach (string[] row in body.Take(6))
        {
            Console.WriteLine("  " + string.Join(" | ", row.Select(value => value.Length > 28 ? value[..25] + "..." : value)));
        }
    }

    Console.WriteLine();
    Console.WriteLine($"Výstup: {outDir}");
}

static List<int> ParsePages(string? text, int pageCount)
{
    // "3-5" -> [3, 4, 5], "20" -> [20], null -> všetky strany. Strany sú od 1.
    if (string.IsNullOrEmpty(text))
    {
        return Enumerable.Range(1, pageCount).ToList();
    }

    string[] bounds = text.Split('-', 2);
    int first = int.Parse(bounds[0]);
    int last = Math.Min(bounds.Length > 1 ? int.Parse(bounds[1]) : first, pageCount);
    return Enumerable.Range(first, last - first + 1).ToList();
}

static byte[] CutPages(string pdfPath, IEnumerable<int> pages)
{
    // Skopíruje vybrané strany do nového PDF bez zmeny obsahu (textová vrstva zostane).
    using PdfDocument source = PdfDocument.Open(pdfPath);
    PdfDocumentBuilder target = new();
    foreach (int page in pages)
    {
        target.AddPage(source, page);
    }

    return target.Build();
}

static async Task<List<AnalyzedPart>> AnalyzeAsync(string pdf, List<int> pages, Profile profile)
{
    DocumentIntelligenceClient client = profile.Login switch
    {
        "entra" => new DocumentIntelligenceClient(new Uri(profile.Endpoint), new DefaultAzureCredential()),
        "az-kluc" => new DocumentIntelligenceClient(new Uri(profile.Endpoint), new AzureKeyCredential(KeyFromAz(profile))),
        "env" => new DocumentIntelligenceClient(new Uri(profile.Endpoint), new AzureKeyCredential(
            Environment.GetEnvironmentVariable("AZURE_DI_KEY") ?? throw new UsageException("Chýba premenná AZURE_DI_KEY"))),
        _ => throw new UsageException($"Neznámy spôsob prihlásenia: {profile.Login}"),
    };

    long limitBytes = profile.MaxSizeMb * 1024L * 1024L;
    List<AnalyzedPart> parts = [];
    foreach (int[] chunk in pages.Chunk(profile.MaxPagesPerCall))
    {
        byte[] payload = CutPages(pdf, chunk);
        if (payload.Length > limitBytes)
        {
            throw new UsageException($"Strany {string.Join(", ", chunk)} majú {payload.Length / 1048576.0:0.0} MB, " +
                $"profil dovoľuje {profile.MaxSizeMb} MB. Skús menej strán alebo profil standard.");
        }

        if (parts.Count > 0)
        {
            await Task.Delay(TimeSpan.FromSeconds(1)); // F0 dovoľuje 1 požiadavku za sekundu
        }

        AnalyzeDocumentOptions options = new("prebuilt-layout", BinaryData.FromBytes(payload))
        {
            OutputContentFormat = DocumentContentFormat.Markdown,
        };
        Operation<AnalyzeResult> operation = await client.AnalyzeDocumentAsync(WaitUntil.Completed, options);
        parts.Add(new AnalyzedPart([.. chunk], operation.Value));
        Console.WriteLine($"  dávka strán {chunk[0]}–{chunk[^1]} hotová");
    }

    return parts;
}

static string KeyFromAz(Profile profile)
{
    // Vypýta kľúč prostriedku cez Azure CLI. Kľúč ostane iba v pamäti tohto procesu.
    string az = FindOnPath(OperatingSystem.IsWindows() ? "az.cmd" : "az")
        ?? throw new UsageException("Azure CLI (az) sa nenašlo, prihlás sa cez `az login` alebo použi prihlasenie = \"env\"");
    ProcessStartInfo startInfo = new(az)
    {
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        UseShellExecute = false,
    };
    string[] arguments =
    [
        "cognitiveservices", "account", "keys", "list",
        "--name", profile.Resource, "--resource-group", profile.ResourceGroup,
        "--subscription", profile.Subscription, "--query", "key1", "--output", "tsv",
    ];
    foreach (string argument in arguments)
    {
        startInfo.ArgumentList.Add(argument);
    }

    using Process process = Process.Start(startInfo)!;
    Task<string> error = process.StandardError.ReadToEndAsync();
    string output = process.StandardOutput.ReadToEnd().Trim();
    process.WaitForExit();
    if (process.ExitCode != 0 || output.Length == 0)
    {
        throw new UsageException($"Kľúč sa nepodarilo získať cez az: {error.Result.Trim()}");
    }

    return output;
}

static string? FindOnPath(string fileName) =>
    (Environment.GetEnvironmentVariable("PATH") ?? "")
        .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
        .Select(directory => Path.Combine(directory, fileName))
        .FirstOrDefault(File.Exists);

static (string[] Header, List<string[]> Body) TableToRows(DocumentTable table)
{
    // Mriežka tabuľky. Riadky nad prvou dátovou bunkou tvoria názvy stĺpcov.
    // Hlavička sa neurčuje podľa cell.Kind samotného: Azure označí ako columnHeader aj popis v prvom
    // stĺpci, ktorý sa ťahá cez celú skupinu riadkov (Rigips), a tie riadky by sa stratili.
    string[][] grid = Enumerable.Range(0, table.RowCount).Select(_ => new string[table.ColumnCount]).ToArray();
    foreach (string[] row in grid)
    {
        Array.Fill(row, "");
    }

    int headerRows = table.Cells
        .Where(cell => cell.Kind is null || cell.Kind == DocumentTableCellKind.Content)
        .Select(cell => cell.RowIndex)
        .DefaultIfEmpty(0)
        .Min();

    foreach (DocumentTableCell cell in table.Cells)
    {
        string text = (cell.Content ?? "").Replace("\n", " ").Trim();
        int lastRow = Math.Min(cell.RowIndex + (cell.RowSpan ?? 1), table.RowCount);
        int lastColumn = Math.Min(cell.ColumnIndex + (cell.ColumnSpan ?? 1), table.ColumnCount);
        for (int row = cell.RowIndex; row < lastRow; row++)
        {
            for (int column = cell.ColumnIndex; column < lastColumn; column++)
            {
                grid[row][column] = text;
            }
        }
    }

    string[] header = new string[table.ColumnCount];
    for (int column = 0; column < table.ColumnCount; column++)
    {
        List<string> parts = [];
        for (int row = 0; row < headerRows; row++)
        {
            string text = grid[row][column];
            if (text.Length > 0 && (parts.Count == 0 || parts[^1] != text))
            {
                parts.Add(text);
            }
        }

        header[column] = parts.Count > 0 ? string.Join(".", parts) : $"stlpec_{column + 1}";
    }

    return (header, grid.Skip(headerRows).ToList());
}

static void WriteCsv(string path, string[] header, List<string[]> body)
{
    static string Quote(string value) =>
        value.IndexOfAny([',', '"', '\n', '\r']) >= 0 ? "\"" + value.Replace("\"", "\"\"") + "\"" : value;

    StringBuilder csv = new();
    foreach (string[] row in body.Prepend(header))
    {
        csv.Append(string.Join(",", row.Select(Quote))).Append("\r\n");
    }

    // utf-8 s BOM, rovnako ako pandas encoding="utf-8-sig" v Pythone (Excel potom zobrazí diakritiku)
    File.WriteAllText(path, csv.ToString(), new UTF8Encoding(encoderShouldEmitUTF8Identifier: true));
}

static string SaveJson(string profileName, List<AnalyzedPart> parts)
{
    JsonObject root = new()
    {
        ["profil"] = profileName,
        ["casti"] = new JsonArray(parts.Select(part => (JsonNode)new JsonObject
        {
            ["strany"] = new JsonArray(part.Pages.Select(page => (JsonNode)page).ToArray()),
            ["vysledok"] = JsonNode.Parse(ModelReaderWriter.Write(part.Result).ToString()),
        }).ToArray()),
    };
    return root.ToJsonString(new JsonSerializerOptions
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    });
}

static List<AnalyzedPart> LoadSaved(string path, List<int> pages)
{
    JsonObject root = JsonNode.Parse(File.ReadAllText(path))!.AsObject();
    if (!root.ContainsKey("casti"))
    {
        // starší formát: jedna odpoveď pre všetky strany
        return [new AnalyzedPart(pages, ReadResult(root))];
    }

    return root["casti"]!.AsArray()
        .Select(part => new AnalyzedPart(
            part!["strany"]!.AsArray().Select(page => page!.GetValue<int>()).ToList(),
            ReadResult(part["vysledok"]!)))
        .ToList();

    static AnalyzeResult ReadResult(JsonNode node) =>
        ModelReaderWriter.Read<AnalyzeResult>(BinaryData.FromString(node.ToJsonString()))!;
}

static string SourceDirectory([CallerFilePath] string path = "") => Path.GetDirectoryName(path)!;

internal sealed record AnalyzedPart(List<int> Pages, AnalyzeResult Result);

internal sealed class UsageException(string message) : Exception(message);

internal sealed record Arguments(string Pdf, string? Pages, string? ProfileName, string ConfigPath, bool FromJson)
{
    public static Arguments Parse(string[] args, string defaultConfig)
    {
        string? pdf = null, pages = null, profile = null;
        string config = defaultConfig;
        bool fromJson = false;
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--strany": pages = Value(args, ref i); break;
                case "--profil": profile = Value(args, ref i); break;
                case "--konfig": config = Value(args, ref i); break;
                case "--z-json": fromJson = true; break;
                default: pdf = args[i]; break;
            }
        }

        return new Arguments(
            pdf ?? throw new UsageException("Použitie: <pdf> [--strany 3-5] [--profil free|standard] [--konfig cesta] [--z-json]"),
            pages, profile, config, fromJson);
    }

    private static string Value(string[] args, ref int index) =>
        ++index < args.Length ? args[index] : throw new UsageException($"Prepínač {args[index - 1]} potrebuje hodnotu");
}

internal sealed record Profile(
    string Description,
    string Endpoint,
    string Subscription,
    string ResourceGroup,
    string Resource,
    string Login,
    int MaxPagesPerCall,
    int MaxSizeMb,
    double PricePer1000Eur)
{
    public static (string Name, Profile Profile) Load(string configPath, string? name)
    {
        TomlTable config = TomlSerializer.Deserialize<TomlTable>(File.ReadAllText(configPath))!;
        TomlTable profiles = (TomlTable)config["profily"];
        name ??= (string)config["aktivny"];
        if (!profiles.TryGetValue(name, out object? value) || value is not TomlTable table)
        {
            throw new UsageException($"Profil '{name}' nie je v {configPath}, dostupné: {string.Join(", ", profiles.Keys)}");
        }

        return (name, new Profile(
            (string)table["popis"],
            (string)table["endpoint"],
            (string)table["predplatne"],
            (string)table["skupina"],
            (string)table["prostriedok"],
            table.TryGetValue("prihlasenie", out object? login) ? (string)login : "entra",
            Convert.ToInt32(table["max_stran_na_volanie"]),
            Convert.ToInt32(table["max_velkost_mb"]),
            Convert.ToDouble(table["cena_za_1000_stran_eur"])));
    }
}
