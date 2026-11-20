use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024;
const SKIP_DIRS: [&str; 5] = [".git", ".hg", ".svn", ".venv", "node_modules"];

fn usage() -> ! {
    eprintln!("usage: trina-native <version|read|search> ...");
    std::process::exit(2)
}

fn read_text(path: &Path) -> Result<String, String> {
    let metadata = fs::metadata(path).map_err(|err| format!("{}: {}", path.display(), err))?;
    if !metadata.is_file() {
        return Err(format!("不是普通文件: {}", path.display()));
    }
    if metadata.len() > MAX_FILE_BYTES {
        return Err(format!("文件超过 2MiB: {}", path.display()));
    }
    let bytes = fs::read(path).map_err(|err| format!("{}: {}", path.display(), err))?;
    if bytes[..bytes.len().min(8192)].contains(&0) {
        return Err(format!("二进制文件: {}", path.display()));
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn command_read(args: &[String]) -> Result<(), String> {
    if args.len() != 4 {
        return Err("read 需要 path start_line line_count".to_string());
    }
    let path = Path::new(&args[1]);
    let start: usize = args[2].parse().map_err(|_| "start_line 必须是整数".to_string())?;
    let count: usize = args[3].parse().map_err(|_| "line_count 必须是整数".to_string())?;
    if start < 1 || count == 0 || count > 1000 {
        return Err("行号范围非法".to_string());
    }
    let text = read_text(path)?;
    let lines: Vec<&str> = text.lines().collect();
    let end = (start - 1 + count).min(lines.len());
    println!("@total={}", lines.len());
    for (offset, line) in lines[(start - 1).min(lines.len())..end].iter().enumerate() {
        println!("{:>6}  {}", start + offset, line);
    }
    Ok(())
}

fn should_skip(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .map(|name| SKIP_DIRS.contains(&name) || name.starts_with('.') || name == "__pycache__")
        .unwrap_or(false)
}

fn walk_search(root: &Path, query: &str, case_sensitive: bool, limit: usize,
               matches: &mut Vec<String>) -> Result<(), String> {
    walk_search_inner(root, root, query, case_sensitive, limit, matches)
}

fn walk_search_inner(base: &Path, current: &Path, query: &str, case_sensitive: bool, limit: usize,
                     matches: &mut Vec<String>) -> Result<(), String> {
    if matches.len() >= limit {
        return Ok(());
    }
    let entries = fs::read_dir(current).map_err(|err| format!("{}: {}", current.display(), err))?;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if !should_skip(&path) {
                walk_search_inner(base, &path, query, case_sensitive, limit, matches)?;
            }
            continue;
        }
        if matches.len() >= limit || should_skip(&path) {
            continue;
        }
        let metadata = match path.metadata() {
            Ok(value) => value,
            Err(_) => continue,
        };
        if metadata.len() > MAX_FILE_BYTES {
            continue;
        }
        let bytes = match fs::read(&path) {
            Ok(value) => value,
            Err(_) => continue,
        };
        if bytes[..bytes.len().min(8192)].contains(&0) {
            continue;
        }
        let text = String::from_utf8_lossy(&bytes);
        for (line_index, line) in text.lines().enumerate() {
            let matched = if case_sensitive {
                line.contains(query)
            } else {
                line.to_lowercase().contains(&query.to_lowercase())
            };
            if !matched {
                continue;
            }
            let column = if case_sensitive {
                line.find(query).unwrap_or(0)
            } else {
                line.to_lowercase().find(&query.to_lowercase()).unwrap_or(0)
            };
            let relative = path.strip_prefix(base).unwrap_or(&path).to_string_lossy().replace('\\', "/");
            let safe_line = line.replace('\t', "\\t").replace('\r', "\\r");
            matches.push(format!("{}\t{}\t{}\t{}", relative, line_index + 1, column + 1, safe_line));
            if matches.len() >= limit {
                break;
            }
        }
    }
    Ok(())
}

fn command_search(args: &[String]) -> Result<(), String> {
    if args.len() != 4 && args.len() != 5 {
        return Err("search 需要 root query max_results [case_sensitive]".to_string());
    }
    let root = PathBuf::from(&args[1]);
    let query = &args[2];
    let limit: usize = args[3].parse().map_err(|_| "max_results 必须是整数".to_string())?;
    let case_sensitive = args.get(4).map(|value| value == "1").unwrap_or(false);
    if query.is_empty() || limit == 0 || limit > 500 {
        return Err("搜索参数非法".to_string());
    }
    let mut matches = Vec::new();
    walk_search(&root, query, case_sensitive, limit, &mut matches)?;
    for item in matches {
        println!("{}", item);
    }
    Ok(())
}

fn atomic_write(path: &Path, content: &str) -> Result<(), String> {
    let temporary = path.with_extension("trina-native-tmp");
    fs::write(&temporary, content.as_bytes()).map_err(|err| err.to_string())?;
    if let Err(error) = fs::rename(&temporary, path) {
        let _ = fs::remove_file(path);
        fs::rename(&temporary, path).map_err(|_| error.to_string())?;
    }
    Ok(())
}

fn command_write(args: &[String]) -> Result<(), String> {
    if args.len() != 3 {
        return Err("write 需要 path content".to_string());
    }
    if args[2].as_bytes().len() > 1_000_000 {
        return Err("content 超过 1MiB".to_string());
    }
    atomic_write(Path::new(&args[1]), &args[2])
}

fn command_edit(args: &[String]) -> Result<(), String> {
    if args.len() != 5 {
        return Err("edit 需要 path old new expected".to_string());
    }
    let expected: usize = args[4].parse().map_err(|_| "expected 必须是整数".to_string())?;
    if expected == 0 {
        return Err("expected 必须大于 0".to_string());
    }
    let source = read_text(Path::new(&args[1]))?;
    let matches = source.matches(&args[2]).count();
    if matches != expected {
        return Err(format!("匹配次数为 {}，期望 {}；未修改", matches, expected));
    }
    let updated = source.replacen(&args[2], &args[3], expected);
    if updated.as_bytes().len() > 1_000_000 {
        return Err("修改后文件超过 1MiB".to_string());
    }
    atomic_write(Path::new(&args[1]), &updated)
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        usage();
    }
    let result = match args[1].as_str() {
        "version" => {
            println!("trina-native 0.5.0");
            Ok(())
        }
        "read" => command_read(&args[1..]),
        "search" => command_search(&args[1..]),
        "write" => command_write(&args[1..]),
        "edit" => command_edit(&args[1..]),
        _ => Err("未知子命令".to_string()),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{}", error);
            ExitCode::from(1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_text_rejects_binary_and_reads_utf8() {
        let root = env::temp_dir().join(format!("trina-native-test-{}", std::process::id()));
        fs::create_dir_all(&root).expect("create test directory");
        let text = root.join("sample.txt");
        fs::write(&text, "alpha\nβeta\n").expect("write text");
        assert!(read_text(&text).expect("read text").contains("βeta"));
        let binary = root.join("sample.bin");
        fs::write(&binary, [0_u8, 1_u8, 2_u8]).expect("write binary");
        assert!(read_text(&binary).is_err());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn search_skips_hidden_and_dependency_directories() {
        let root = env::temp_dir().join(format!("trina-native-search-{}", std::process::id()));
        fs::create_dir_all(root.join(".git")).expect("create hidden directory");
        fs::create_dir_all(root.join("src")).expect("create source directory");
        fs::write(root.join(".git/hidden.txt"), "needle").expect("write hidden");
        fs::write(root.join("src/main.txt"), "needle here\n").expect("write source");
        let mut matches = Vec::new();
        walk_search(&root, "needle", false, 10, &mut matches).expect("search");
        assert_eq!(matches.len(), 1);
        assert!(matches[0].contains("src/main.txt"));
        assert!(matches[0].contains("\t1\t1\t"));
        let _ = fs::remove_dir_all(root);
    }
}
