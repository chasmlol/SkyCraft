#include "Game.h"

#include <ExDisp.h>
#include <TlHelp32.h>
#include <ShlDisp.h>
#include <ShlObj.h>
#include <SimpleIni.h>
#include <servprov.h>
#include <wrl/client.h>

#include <filesystem>
#include <format>
#include <fstream>

// Minecraft starts with Skyrim. The SkyCraft Fabric mod then waits on its title screen until
// Skyrim's world is up, hides its own window and opens the SkyCraft world by itself, and quits
// again when this Skyrim closes. What to start comes from Data/SKSE/Plugins/SkyCraft.ini; by
// default Prism Launcher's "SkyCraft" instance (Prism signs in to the player's Microsoft account;
// the official launcher can't be started into a profile from outside).
namespace skycraft::Launcher
{
	namespace
	{
		std::wstring Widen(const std::string& a_utf8)
		{
			if (a_utf8.empty()) {
				return {};
			}
			const int n = ::MultiByteToWideChar(CP_UTF8, 0, a_utf8.data(), static_cast<int>(a_utf8.size()), nullptr, 0);
			std::wstring out(static_cast<std::size_t>(n), L'\0');
			::MultiByteToWideChar(CP_UTF8, 0, a_utf8.data(), static_cast<int>(a_utf8.size()), out.data(), n);
			return out;
		}

		std::wstring ExpandEnv(const std::wstring& a_path)
		{
			wchar_t buf[MAX_PATH * 2];
			const DWORD n = ::ExpandEnvironmentStringsW(a_path.c_str(), buf, static_cast<DWORD>(std::size(buf)));
			return n > 0 && n <= std::size(buf) ? std::wstring(buf) : a_path;
		}

		// ---- Linux bridge (additive; auto-start below is untouched) ---------------------
		// Same PC, Skyrim via Proton/Wine + native Linux Minecraft (see Link.cpp): this
		// Win32 auto-start can't launch a native Linux Prism, and the mutex/process scans
		// can't see host processes, so bridge mode is manual-start (like bStartWithSkyrim
		// = 0). Mirrors Link.cpp's opt-in: SKYCRAFT_LINUX_BRIDGE=1 (or a path), or
		// SKYCRAFT_LINK_FILE (the same env the Java side reads) set.
		bool LinuxBridgeActive()
		{
			const auto get = [](const wchar_t* a_name) {
				const DWORD need = ::GetEnvironmentVariableW(a_name, nullptr, 0);
				if (need == 0) {
					return std::wstring{};
				}
				std::wstring out(need - 1, L'\0');
				::GetEnvironmentVariableW(a_name, out.data(), need);
				return out;
			};
			const auto bridge = get(L"SKYCRAFT_LINUX_BRIDGE");
			if (!bridge.empty() && bridge != L"0") {
				return true;
			}
			return !get(L"SKYCRAFT_LINK_FILE").empty();
		}

		// Runs a program the way double-clicking it would: started by the desktop's Explorer, not by
		// Skyrim. Under Mod Organizer that keeps Minecraft out of MO2's virtual file system and off
		// the list of processes MO2 waits for (it stays locked until they've all exited).
		bool OpenFromDesktop(const std::wstring& a_file, const std::wstring& a_args, const std::wstring& a_dir, int a_show)
		{
			using Microsoft::WRL::ComPtr;
			ComPtr<IShellWindows> windows;
			if (FAILED(::CoCreateInstance(CLSID_ShellWindows, nullptr, CLSCTX_LOCAL_SERVER, IID_PPV_ARGS(&windows)))) {
				return false;
			}
			VARIANT location{};
			location.vt = VT_I4;
			location.lVal = CSIDL_DESKTOP;
			VARIANT           empty{};
			long              hwnd = 0;
			ComPtr<IDispatch> desktop;
			if (FAILED(windows->FindWindowSW(&location, &empty, SWC_DESKTOP, &hwnd, SWFO_NEEDDISPATCH, &desktop)) || !desktop) {
				return false;
			}
			ComPtr<IServiceProvider> services;
			ComPtr<IShellBrowser>    browser;
			ComPtr<IShellView>       view;
			ComPtr<IDispatch>        background;
			ComPtr<IShellFolderViewDual> folderView;
			ComPtr<IDispatch>        application;
			ComPtr<IShellDispatch2>  shell;
			if (FAILED(desktop.As(&services)) || FAILED(services->QueryService(SID_STopLevelBrowser, IID_PPV_ARGS(&browser))) ||
				FAILED(browser->QueryActiveShellView(&view)) || FAILED(view->GetItemObject(SVGIO_BACKGROUND, IID_PPV_ARGS(&background))) ||
				FAILED(background.As(&folderView)) || FAILED(folderView->get_Application(&application)) || FAILED(application.As(&shell))) {
				return false;
			}
			BSTR    file = ::SysAllocString(a_file.c_str());
			VARIANT args{}, dir{}, operation{}, show{};
			args.vt = dir.vt = operation.vt = VT_BSTR;
			args.bstrVal = ::SysAllocString(a_args.c_str());
			dir.bstrVal = ::SysAllocString(a_dir.c_str());
			operation.bstrVal = ::SysAllocString(L"open");
			show.vt = VT_I4;
			show.lVal = a_show;
			const HRESULT hr = shell->ShellExecute(file, args, dir, operation, show);
			::SysFreeString(file);
			::VariantClear(&args);
			::VariantClear(&dir);
			::VariantClear(&operation);
			return SUCCEEDED(hr);
		}

		std::filesystem::path FindPrism()
		{
			for (const wchar_t* candidate : { L"%LOCALAPPDATA%\\Programs\\PrismLauncher\\prismlauncher.exe", L"%ProgramFiles%\\PrismLauncher\\prismlauncher.exe" }) {
				std::filesystem::path p = ExpandEnv(candidate);
				if (std::filesystem::exists(p)) {
					return p;
				}
			}
			return {};
		}
	}

	namespace
	{
		std::atomic<Status> status{ Status::kOff };

		// The Minecraft SkyCraft ships: portable Prism Launcher with a ready "SkyCraft" instance.
		const std::filesystem::path kBundle = "Data/SKSE/Plugins/SkyCraft/SkyCraft-Minecraft.zip";

		std::filesystem::path InstallDir() { return ExpandEnv(L"%LOCALAPPDATA%\\SkyCraft"); }

		// Unpacks the bundle to %LOCALAPPDATA%\SkyCraft (outside Skyrim and Mod Organizer) the first
		// time, and again whenever this SkyCraft brings a different one. Prism's own data there (the
		// signed-in account, downloaded Minecraft and Java, the SkyCraft world) is kept; the instance
		// and its SkyCraft, Fabric API and e4mc jars are replaced, so both halves always match.
		std::filesystem::path EnsureBundle()
		{
			const auto      dir = InstallDir();
			const auto      prism = dir / "Prism" / "prismlauncher.exe";
			std::error_code ec;
			const auto      stamp = std::format("{} {}", std::filesystem::file_size(kBundle, ec),
					 std::filesystem::last_write_time(kBundle, ec).time_since_epoch().count());
			std::string     installed;
			if (std::ifstream in{ dir / "bundle.stamp" }; in) {
				std::getline(in, installed);
			}
			if (installed == stamp && std::filesystem::exists(prism)) {
				return prism;
			}
			logger::info("Minecraft: unpacking SkyCraft's Minecraft to {}", dir.string());
			std::filesystem::create_directories(dir, ec);
			for (const auto& entry : std::filesystem::directory_iterator(dir / "Prism" / "instances" / "SkyCraft" / ".minecraft" / "mods", ec)) {
				const auto name = entry.path().filename().string();
				if (name.starts_with("skycraft-") || name.starts_with("fabric-api-") || name.starts_with("e4mc-")) {
					std::filesystem::remove(entry.path(), ec);
				}
			}
			// A real copy first (Skyrim may be seeing the bundle through Mod Organizer's virtual files).
			const auto copy = dir / "bundle.zip";
			if (!std::filesystem::copy_file(kBundle, copy, std::filesystem::copy_options::overwrite_existing, ec)) {
				logger::warn("Minecraft: couldn't copy {} ({})", kBundle.string(), ec.message());
				return {};
			}
			std::wstring        command = L"\"" + ExpandEnv(L"%SystemRoot%\\System32\\tar.exe") + L"\" -xf \"" + copy.wstring() + L"\" -C \"" + dir.wstring() + L"\"";
			STARTUPINFOW        si{ sizeof(si) };
			PROCESS_INFORMATION pi{};
			DWORD               code = 1;
			if (::CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr, dir.c_str(), &si, &pi)) {
				::WaitForSingleObject(pi.hProcess, 5 * 60 * 1000);
				::GetExitCodeProcess(pi.hProcess, &code);
				::CloseHandle(pi.hThread);
				::CloseHandle(pi.hProcess);
			}
			std::filesystem::remove(copy, ec);
			if (code != 0 || !std::filesystem::exists(prism)) {
				logger::warn("Minecraft: unpacking failed (tar exit code {})", code);
				return {};
			}
			// Prism's settings: only the first time (after that they're the player's).
			const auto cfg = dir / "Prism" / "prismlauncher.cfg";
			if (!std::filesystem::exists(cfg)) {
				std::filesystem::copy_file(dir / "defaults" / "prismlauncher.cfg", cfg, ec);
			} else {
				// Older bundles: Prism's "not enough free RAM" question popped up over Skyrim (Windows
				// counts its file cache as used, and Skyrim is loading at that moment). Turn it off.
				std::ifstream in(cfg, std::ios::binary);
				std::string   text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
				in.close();
				if (text.find("LowMemWarning=") == std::string::npos) {
					const auto at = text.find("[General]");
					if (at != std::string::npos) {
						const auto eol = text.find('\n', at);
						text.insert(eol == std::string::npos ? text.size() : eol + 1, "LowMemWarning=false\n");
					} else {
						text += "\n[General]\nLowMemWarning=false\n";
					}
					std::ofstream(cfg, std::ios::binary | std::ios::trunc) << text;
				}
			}
			std::ofstream(dir / "bundle.stamp") << stamp;
			return prism;
		}

		// Runs a program the way double-clicking it would (see OpenFromDesktop), or directly if
		// there's no desktop shell to ask. Batch files go through cmd, without a console window.
		bool Start(const std::filesystem::path& a_program, const std::wstring& a_args, const std::string& a_shown)
		{
			const auto         ext = a_program.extension().wstring();
			const bool         script = _wcsicmp(ext.c_str(), L".bat") == 0 || _wcsicmp(ext.c_str(), L".cmd") == 0;
			const std::wstring dir = a_program.parent_path().wstring();
			const HRESULT      com = ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
			const bool         viaDesktop = OpenFromDesktop(a_program.wstring(), a_args, dir, script ? SW_HIDE : SW_SHOWNORMAL);
			if (SUCCEEDED(com)) {
				::CoUninitialize();
			}
			if (viaDesktop) {
				logger::info("Minecraft: started {}", a_shown);
				return true;
			}
			std::wstring command = script ? L"cmd.exe /c \"\"" + a_program.wstring() + L"\" " + a_args + L"\"" : L"\"" + a_program.wstring() + L"\" " + a_args;
			STARTUPINFOW        si{ sizeof(si) };
			PROCESS_INFORMATION pi{};
			if (!::CreateProcessW(nullptr, command.data(), nullptr, nullptr, FALSE, script ? CREATE_NO_WINDOW : 0, nullptr, dir.c_str(), &si, &pi)) {
				logger::warn("Minecraft: couldn't start {} (error {})", a_shown, ::GetLastError());
				return false;
			}
			::CloseHandle(pi.hThread);
			::CloseHandle(pi.hProcess);
			logger::info("Minecraft: started {} (directly)", a_shown);
			return true;
		}
	}

	Status GetStatus() { return status.load(); }

	// A Minecraft with the SkyCraft mod holds this mutex while it runs (SkyLink.announceRunning).
	bool MinecraftRunning()
	{
		HANDLE mutex = ::OpenMutexW(SYNCHRONIZE, FALSE, L"Local\\SkyCraft_v1_minecraft");
		if (mutex) {
			::CloseHandle(mutex);
			return true;
		}
		return false;
	}

	bool PrismRunning()
	{
		HANDLE snapshot = ::CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
		if (snapshot == INVALID_HANDLE_VALUE) {
			return false;
		}
		PROCESSENTRY32W entry{ sizeof(entry) };
		bool            found = false;
		for (BOOL more = ::Process32FirstW(snapshot, &entry); more && !found; more = ::Process32NextW(snapshot, &entry)) {
			found = _wcsicmp(entry.szExeFile, L"prismlauncher.exe") == 0;
		}
		::CloseHandle(snapshot);
		return found;
	}
}

namespace skycraft
{
	bool DiagnosticsEnabled()
	{
		static const bool on = [] {
			CSimpleIniA ini;
			ini.SetUnicode();
			ini.LoadFile("Data/SKSE/Plugins/SkyCraft.ini");
			return ini.GetBoolValue("Debug", "bDiagnostics", false);
		}();
		return on;
	}
}

namespace skycraft::Launcher
{

	void StartMinecraft()
	{
		// Linux bridge: never auto-start from Wine (see above); the player starts the native
		// Linux Prism/Minecraft themselves. Status stays kOff, like bStartWithSkyrim = 0, so
		// the corner diagnostics stay silent instead of reporting Windows-only checks.
		if (LinuxBridgeActive()) {
			logger::info("Minecraft: Linux bridge active: start the native Linux Minecraft yourself; auto-start disabled");
			return;
		}
		CSimpleIniA ini;
		ini.SetUnicode();
		const bool haveIni = ini.LoadFile("Data/SKSE/Plugins/SkyCraft.ini") >= 0;
		if (!ini.GetBoolValue("Minecraft", "bStartWithSkyrim", true)) {
			logger::info("Minecraft: not started with Skyrim (bStartWithSkyrim = 0)");
			return;
		}
		if (MinecraftRunning()) {
			logger::info("Minecraft: already running");
			status = Status::kRunning;
			// It may be the last Skyrim's Minecraft on its way out (it quits a few seconds after that
			// Skyrim closes, and this Skyrim only takes it over once its data has loaded): if it goes
			// in the next minute, start one for this Skyrim.
			std::thread([] {
				for (int i = 0; i < 60; ++i) {
					std::this_thread::sleep_for(1s);
					if (!MinecraftRunning()) {
						logger::info("Minecraft: the one that was running has quit; starting another");
						std::this_thread::sleep_for(3s);  // let its launcher close too
						StartMinecraft();
						return;
					}
				}
			}).detach();
			return;
		}
		const std::filesystem::path chosen = ExpandEnv(Widen(ini.GetValue("Minecraft", "sLauncher", "")));
		const std::wstring          args = Widen(ini.GetValue("Minecraft", "sArguments", "--launch SkyCraft"));
		const std::string           argsShown = ini.GetValue("Minecraft", "sArguments", "--launch SkyCraft");
		const bool                  bundled = chosen.empty() && std::filesystem::exists(kBundle);
		const std::filesystem::path installed = chosen.empty() && !bundled ? FindPrism() : std::filesystem::path{};
		if (chosen.empty() && !bundled && installed.empty()) {
			logger::warn("Minecraft: not started: no SkyCraft-Minecraft.zip next to the plugin and no Prism Launcher where its installer puts it; set sLauncher in SkyCraft.ini{}",
				haveIni ? "" : " (no SkyCraft.ini found)");
			status = Status::kNoLauncher;
			return;
		}
		if (!chosen.empty() && !std::filesystem::exists(chosen)) {
			logger::warn("Minecraft: not started: {} doesn't exist (sLauncher in SkyCraft.ini)", chosen.string());
			status = Status::kNoLauncher;
			return;
		}
		status = Status::kStarting;
		// Off the main thread: unpacking and talking to Explorer take a moment.
		std::thread([chosen, installed, bundled, args, argsShown] {
			std::filesystem::path program = !chosen.empty() ? chosen : installed;
			if (bundled) {
				program = EnsureBundle();
				if (program.empty()) {
					status = Status::kFailed;
					return;
				}
				if (!std::filesystem::exists(program.parent_path() / "accounts.json")) {
					status = Status::kSignIn;  // first time: Prism asks for the Microsoft account
				}
			}
			if (!Start(program, args, program.string() + " " + argsShown)) {
				status = Status::kFailed;
			}
		}).detach();
	}
}
