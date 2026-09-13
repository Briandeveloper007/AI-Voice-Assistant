# Harley Voice Assistant: Custom Wake Word Setup

To enable the custom `hey_harley` wake word in the standalone application, you must generate a synthetic `.onnx` model using the **openWakeWord** platform. 

## Generation Instructions

openWakeWord provides the capability to synthetically train custom wake word models from text, which eliminates the need to record thousands of hours of audio.

1. **Access the openWakeWord Tools:**
   Navigate to the [openWakeWord GitHub Repository](https://github.com/dscripka/openWakeWord).
   
2. **Train the Model:**
   You can easily train a synthetic model using their provided Google Colab notebook (linked in their README) or by using their local Python training scripts.
   Set the target phrase to `"Hey Harley"`.

3. **Export as ONNX:**
   Once the training script completes, it will output a `.onnx` model file representing your custom wake word.

4. **Rename the File:**
   Ensure the final model file is named exactly `hey_harley.onnx`.

## Placement and Building

Before running the build script, you must place the model in the correct directory so that PyInstaller can bundle it into the standalone executable.

1. Locate or create the `models/` directory in the root of the `harley` project.
2. Place the model file inside: `harley/models/hey_harley.onnx`

```text
harley/
├── app/
├── voice/
├── models/
│   └── hey_harley.onnx    <-- Place it here
├── build.ps1
└── ...
```

## Compilation

Once the model is placed, simply run the PowerShell build script from the project root:

```powershell
.\build.ps1
```

PyInstaller will automatically detect `models/hey_harley.onnx` via the `--add-data` flag and bundle it directly into the resulting `HarleyAssistant.exe`.
