"""
PDF to Markdown converter supporting Marker and MinerU.
Supports both CLI usage and a Gradio web UI.
"""

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path


def convert_pdf_marker(pdf_path: str, output_dir: str | None = None) -> tuple[str, str]:
    """Convert a PDF file to Markdown using Marker."""
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if output_dir is None:
        output_dir = pdf_path.parent
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading Marker models...", flush=True)
    models = create_model_dict()

    print(f"Converting with Marker: {pdf_path.name}", flush=True)
    converter = PdfConverter(artifact_dict=models)
    rendered = converter(str(pdf_path))

    markdown, _, images = text_from_rendered(rendered)

    output_file = output_dir / (pdf_path.stem + ".md")
    output_file.write_text(markdown, encoding="utf-8")

    if images:
        img_dir = output_dir / (pdf_path.stem + "_images")
        img_dir.mkdir(exist_ok=True)
        for img_name, img in images.items():
            img.save(img_dir / img_name)
        print(f"Saved {len(images)} image(s) to {img_dir}")

    print(f"Saved: {output_file}")
    return markdown, str(output_file)


def convert_pdf_mineru(pdf_path: str, output_dir: str | None = None) -> tuple[str, str]:
    """Convert a PDF file to Markdown using MinerU."""
    from mineru.cli.api_client import (
        LocalAPIServer,
        UploadAsset,
        build_http_timeout,
        build_parse_request_form_data,
        download_result_zip,
        safe_extract_zip,
        submit_parse_task_sync,
        wait_for_local_api_ready,
        wait_for_task_result,
    )
    import httpx

    import shutil

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if output_dir is None:
        output_dir = pdf_path.parent
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    # Copy PDF to a short temp name to avoid Windows MAX_PATH issues
    tmp_pdf_dir = tempfile.mkdtemp(prefix="mineru-in-")
    short_name = "input.pdf"
    tmp_pdf = Path(tmp_pdf_dir) / short_name
    shutil.copy2(pdf_path, tmp_pdf)

    print("Starting MinerU local API server...", flush=True)
    server = LocalAPIServer()
    server_url = server.start()

    try:
        upload_assets = [UploadAsset(path=tmp_pdf, upload_name=short_name)]
        form_data = build_parse_request_form_data(
            lang_list=["ch", "en"],
            backend="pipeline",
            parse_method="auto",
            formula_enable=True,
            table_enable=True,
            server_url=None,
            start_page_id=0,
            end_page_id=None,
            return_md=True,
            return_middle_json=False,
            return_model_output=False,
            return_content_list=False,
            return_images=True,
            response_format_zip=True,
            return_original_file=False,
        )

        async def _run():
            async with httpx.AsyncClient(
                timeout=build_http_timeout(), follow_redirects=True
            ) as client:
                await wait_for_local_api_ready(client, server)
                print(f"Converting with MinerU: {pdf_path.name}", flush=True)
                submit_resp = submit_parse_task_sync(server_url, upload_assets, form_data)
                await wait_for_task_result(
                    client, submit_resp, pdf_path.name,
                    status_callback=lambda s: print(f"  Status: {s}", flush=True),
                )
                zip_path = await download_result_zip(client, submit_resp, pdf_path.name)
            return zip_path

        zip_path = asyncio.run(_run())
        # Track existing markdown files so we can identify the file produced by this run.
        existing_md_files = {f.resolve() for f in output_dir.rglob("*.md")}
        safe_extract_zip(zip_path, output_dir)
    finally:
        server.stop()
        shutil.rmtree(tmp_pdf_dir, ignore_errors=True)

    # Find the generated markdown file from this run and rename to original PDF stem
    md_files = [
        f for f in output_dir.rglob("*.md")
        if f.resolve() not in existing_md_files
    ]
    if md_files:
        output_file = md_files[0]
        final_file = output_dir / (pdf_path.stem + ".md")
        if output_file != final_file:
            shutil.move(str(output_file), str(final_file))
            output_file = final_file
        markdown = output_file.read_text(encoding="utf-8")
    else:
        raise RuntimeError("MinerU did not produce a markdown file")

    print(f"Saved: {output_file}")
    return markdown, str(output_file)


def prompt_tool_selection() -> str:
    """Interactive prompt to select conversion tool."""
    print("\n请选择转换工具:")
    print("  1. Marker")
    print("  2. MinerU")
    while True:
        choice = input("\n请输入选项 (1/2): ").strip()
        if choice == "1":
            return "marker"
        elif choice == "2":
            return "mineru"
        print("无效输入，请输入 1 或 2")


def convert_pdf(pdf_path: str, output_dir: str | None = None, tool: str = "marker") -> tuple[str, str]:
    """Dispatch to the selected converter."""
    if tool == "marker":
        return convert_pdf_marker(pdf_path, output_dir)
    elif tool == "mineru":
        return convert_pdf_mineru(pdf_path, output_dir)
    else:
        raise ValueError(f"Unknown tool: {tool}")


def launch_ui():
    """Launch a Gradio web UI for PDF conversion."""
    import gradio as gr

    def gradio_convert(pdf_file, tool_choice):
        if pdf_file is None:
            return "请上传一个PDF文件。", ""
        tool = "marker" if tool_choice == "Marker" else "mineru"
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                markdown, _ = convert_pdf(pdf_file.name, output_dir=tmp_dir, tool=tool)
            return markdown, f"使用 {tool_choice} 转换成功。"
        except Exception as e:
            return "", f"Error: {e}"

    with gr.Blocks(title="PDF → Markdown") as demo:
        gr.Markdown("## PDF to Markdown Converter")
        gr.Markdown("Upload a PDF and convert it to Markdown using Marker or MinerU.")

        with gr.Row():
            with gr.Column():
                pdf_input = gr.File(label="Upload PDF", file_types=[".pdf"])
                tool_radio = gr.Radio(
                    choices=["Marker", "MinerU"],
                    value="Marker",
                    label="转换工具",
                )
                convert_btn = gr.Button("Convert", variant="primary")
                status = gr.Textbox(label="Status", interactive=False)
            with gr.Column():
                md_output = gr.Textbox(
                    label="Markdown Output",
                    lines=30,
                    interactive=False,
                )

        convert_btn.click(
            fn=gradio_convert,
            inputs=[pdf_input, tool_radio],
            outputs=[md_output, status],
        )

    demo.launch()


def main():
    parser = argparse.ArgumentParser(description="Convert PDF to Markdown (Marker / MinerU)")
    subparsers = parser.add_subparsers(dest="command")

    # CLI convert command
    cli = subparsers.add_parser("convert", help="Convert a PDF file from the command line")
    cli.add_argument("pdf", help="Path to the PDF file")
    cli.add_argument("-o", "--output-dir", help="Output directory (default: same as PDF)")
    cli.add_argument(
        "-t", "--tool",
        choices=["marker", "mineru"],
        default=None,
        help="Conversion tool (default: interactive prompt)",
    )

    # UI command
    subparsers.add_parser("ui", help="Launch the Gradio web UI")

    args = parser.parse_args()

    if args.command == "convert":
        tool = args.tool if args.tool else prompt_tool_selection()
        try:
            _, out = convert_pdf(args.pdf, args.output_dir, tool=tool)
            print(f"Done. Output: {out}")
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "ui":
        launch_ui()
    else:
        # Default: launch UI
        launch_ui()


if __name__ == "__main__":
    main()
