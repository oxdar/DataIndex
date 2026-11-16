# Regenerates the binary office fixtures with Microsoft Office on Windows.
#
# The committed fixtures were produced by this script so the readers are tested
# against files written by real Office applications rather than by the readers'
# own assumptions. Content is fictional. Personal information is removed on save.
# Run with PowerShell 7 from any directory: pwsh -File make_office_fixtures.ps1
# (Windows PowerShell 5.1 was observed to block indefinitely inside Word SaveAs2.)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# Office can block on a hidden prompt when saving straight into a synced or
# protected Documents folder, so files are staged in TEMP and copied at the end.
$stage = Join-Path ([IO.Path]::GetTempPath()) ("dataindex-fixtures-" + [guid]::NewGuid())
New-Item -ItemType Directory $stage | Out-Null

function Out-Fixture([string]$name) {
    return Join-Path $stage $name
}

# Word: supply_agreement.{docx,doc,rtf,odt,pdf} and the password-protected protected.docx
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
try {
    $doc = $word.Documents.Add()
    $doc.RemovePersonalInformation = $true
    $sel = $word.Selection
    function Add-Paragraph([int]$style, [string]$text) {
        $sel.Style = $style
        $sel.TypeText($text)
        $sel.TypeParagraph()
    }
    Add-Paragraph -63 "Supply agreement"
    Add-Paragraph -1 "This fictional agreement is a DataIndex test fixture."
    Add-Paragraph -2 "Payment terms"
    Add-Paragraph -1 "Fictional invoices are due within 30 days of delivery."
    Add-Paragraph -3 "Late payment"
    Add-Paragraph -1 "A late fee of 2 percent applies for each month overdue."
    $sel.InsertBreak(7)
    Add-Paragraph -2 "Delivery"
    Add-Paragraph -1 "Goods ship from the North warehouse every Tuesday."
    $sel.Style = -1
    $table = $doc.Tables.Add($sel.Range, 3, 2)
    $table.Cell(1, 1).Range.Text = "Item"
    $table.Cell(1, 2).Range.Text = "Unit price"
    $table.Cell(2, 1).Range.Text = "Widget"
    $table.Cell(2, 2).Range.Text = "3.50"
    $table.Cell(3, 1).Range.Text = "Gadget"
    $table.Cell(3, 2).Range.Text = "12.00"
    $sel.EndKey(6) | Out-Null
    $sel.InsertBreak(7)
    Add-Paragraph -2 "Termination"
    $sel.Style = -1
    $sel.TypeText("Either party may end this fictional agreement with 90 days notice.")

    $doc.SaveAs2((Out-Fixture "supply_agreement.docx"), 16)
    $doc.SaveAs2((Out-Fixture "supply_agreement.doc"), 0)
    $doc.SaveAs2((Out-Fixture "supply_agreement.rtf"), 6)
    $doc.SaveAs2((Out-Fixture "supply_agreement.odt"), 23)
    # PDF with bookmarks created from the headings (wdExportCreateHeadingBookmarks).
    $doc.ExportAsFixedFormat((Out-Fixture "supply_agreement.pdf"), 17, $false, 0, 0, 1, 1, 0, $false, $false, 1)
    # Saved last: Word keeps the password for any later save of this document.
    $doc.SaveAs2((Out-Fixture "protected.docx"), 16, $false, "fixture-password")
    $doc.Close(0)
}
finally {
    $word.Quit()
}

# Excel: inventory.{xlsx,xls,ods}
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
try {
    $book = $excel.Workbooks.Add()
    $book.RemovePersonalInformation = $true
    while ($book.Worksheets.Count -lt 2) {
        $book.Worksheets.Add([Type]::Missing, $book.Worksheets.Item($book.Worksheets.Count)) | Out-Null
    }
    $prices = $book.Worksheets.Item(1)
    $prices.Name = "Prices"
    # Cells are written through Formula strings: Excel parses numbers and
    # booleans natively, and pwsh 7 cannot reliably set Value2 to mixed types.
    $rows = @(
        @("Item", "Unit price", "Updated", "In stock"),
        @("Widget", "3.5", "2026-03-14", "TRUE"),
        @("Gadget", "12", "2026-04-02", "FALSE"),
        @("Sprocket", "0.25", "2026-05-30", "TRUE")
    )
    for ($r = 0; $r -lt $rows.Count; $r++) {
        for ($c = 0; $c -lt 4; $c++) {
            $cell = $prices.Cells.Item($r + 1, $c + 1)
            $value = [string]$rows[$r][$c]
            if ($r -gt 0 -and $c -eq 2) {
                $cell.NumberFormat = "yyyy-mm-dd"
                $value = [string]([datetime]$value - [datetime]"1899-12-30").Days
            }
            $cell.Formula = $value
        }
    }
    $prices.Cells.Item(6, 1).Formula = "Total"
    $prices.Cells.Item(6, 2).Formula = "=SUM(B2:B4)"
    $stock = $book.Worksheets.Item(2)
    $stock.Name = "Stock levels"
    $stock.Cells.Item(1, 1).Formula = "Site"
    $stock.Cells.Item(1, 2).Formula = "Units"
    $stock.Cells.Item(2, 1).Formula = "North warehouse"
    $stock.Cells.Item(2, 2).Formula = "420"
    $stock.Cells.Item(3, 1).Formula = "South depot"
    $stock.Cells.Item(3, 2).Formula = "75"
    $prices.Activate()
    $book.SaveAs((Out-Fixture "inventory.xlsx"), 51)
    $book.SaveAs((Out-Fixture "inventory.xls"), 56)
    $book.SaveAs((Out-Fixture "inventory.ods"), 60)
    $book.Close($false)
}
finally {
    $excel.Quit()
}

# PowerPoint: quarterly_review.{pptx,ppt,odp}
$powerpoint = New-Object -ComObject PowerPoint.Application
try {
    $deck = $powerpoint.Presentations.Add(0)
    $deck.RemovePersonalInformation = -1
    $title = $deck.Slides.Add(1, 1)
    $title.Shapes.Item(1).TextFrame.TextRange.Text = "Quarterly review"
    $title.Shapes.Item(2).TextFrame.TextRange.Text = "Fictional company, third quarter"
    $revenue = $deck.Slides.Add(2, 2)
    $revenue.Shapes.Item(1).TextFrame.TextRange.Text = "Revenue"
    $revenue.Shapes.Item(2).TextFrame.TextRange.Text = "Revenue grew 12 percent`rThe North site led growth"
    $revenue.NotesPage.Shapes.Placeholders.Item(2).TextFrame.TextRange.Text = "Mention the fictional North site expansion."
    $sites = $deck.Slides.Add(3, 11)
    $sites.Shapes.Item(1).TextFrame.TextRange.Text = "Site headcount"
    $grid = $sites.Shapes.AddTable(3, 2, 50, 150, 400, 120).Table
    $grid.Cell(1, 1).Shape.TextFrame.TextRange.Text = "Site"
    $grid.Cell(1, 2).Shape.TextFrame.TextRange.Text = "Staff"
    $grid.Cell(2, 1).Shape.TextFrame.TextRange.Text = "North"
    $grid.Cell(2, 2).Shape.TextFrame.TextRange.Text = "48"
    $grid.Cell(3, 1).Shape.TextFrame.TextRange.Text = "South"
    $grid.Cell(3, 2).Shape.TextFrame.TextRange.Text = "19"
    $deck.SaveAs((Out-Fixture "quarterly_review.pptx"), 24)
    $deck.SaveAs((Out-Fixture "quarterly_review.ppt"), 1)
    $deck.SaveAs((Out-Fixture "quarterly_review.odp"), 35)
    $deck.Close()
}
finally {
    $powerpoint.Quit()
}

Copy-Item -Path (Join-Path $stage "*") -Destination $here -Force
Remove-Item -Recurse -Force $stage
