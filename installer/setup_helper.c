#define _GNU_SOURCE
/* Privileged offline setup boundary. No shell, external sources or user paths. */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

#define PAYLOAD "/usr/share/aquahub/offline"
#define INSTALLER_STATE "/var/lib/aquahub/installer"
#define MAX_PACKAGES 512
static int log_descriptor = -1;

static void write_all(int descriptor, const char *data, size_t length) {
    while (length) {
        ssize_t count = write(descriptor, data, length);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) break;
        data += count;
        length -= (size_t)count;
    }
}

static void phase(const char *message) {
    puts(message);
    fflush(stdout);
    if (log_descriptor >= 0) dprintf(log_descriptor, "%s\n", message);
}

static void die(const char *message) {
    fprintf(stderr, "AquaHub : %s\n", message);
    if (log_descriptor >= 0) {
        dprintf(log_descriptor, "AquaHub : %s\n", message);
        fsync(log_descriptor);
    }
    exit(EXIT_FAILURE);
}

static void private_directory(const char *path) {
    struct stat item;
    if (mkdir(path, 0700) < 0 && errno != EEXIST) die("Impossible de créer le dossier d’installation.");
    if (lstat(path, &item) || !S_ISDIR(item.st_mode))
        die("Le dossier d’installation n’est pas sûr.");
    if (item.st_uid != 0) {
        /* APT legitimately changes these two private scratch directories to
         * _apt. Their root-only parent prevents replacement by another user. */
        int partial = !strcmp(path, INSTALLER_STATE "/lists/partial") || !strcmp(path, INSTALLER_STATE "/archives/partial");
        struct passwd *apt = partial ? getpwnam("_apt") : NULL;
        if (!apt || item.st_uid != apt->pw_uid || chown(path, 0, 0))
            die("Le dossier d’installation n’est pas sûr.");
    }
    if (chmod(path, 0700)) die("Impossible de protéger le dossier d’installation.");
}

static void trusted_regular(const char *path) {
    struct stat item;
    if (lstat(path, &item) || !S_ISREG(item.st_mode) || item.st_uid != 0 || (item.st_mode & 0022))
        die("Un fichier du kit est absent ou n’est pas protégé. Réinstallez le paquet AquaHub.");
}

static void supported_platform(void) {
    FILE *stream = fopen("/etc/os-release", "re");
    if (!stream) die("Impossible d’identifier la plateforme du poste.");
    char line[512];
    int debian = 0, version13 = 0;
    while (fgets(line, sizeof line, stream)) {
        line[strcspn(line, "\r\n")] = 0;
        if (!strcmp(line, "ID=debian") || !strcmp(line, "ID=\"debian\"")) debian = 1;
        if (!strcmp(line, "VERSION_ID=13") || !strcmp(line, "VERSION_ID=\"13\"")) version13 = 1;
    }
    fclose(stream);
    struct utsname platform;
    if (!debian || !version13 || uname(&platform) || strcmp(platform.machine, "x86_64"))
        die("Ce kit est destiné à Debian 13 amd64. Aucun paquet n’a été installé.");
}

static int run(char *const arguments[]) {
    int output[2];
    if (pipe2(output, O_CLOEXEC)) die("Impossible de suivre l’étape d’installation.");
    pid_t child = fork();
    if (child < 0) die("Impossible de démarrer une étape d’installation.");
    if (child == 0) {
        close(output[0]);
        if (dup2(output[1], STDOUT_FILENO) < 0 || dup2(output[1], STDERR_FILENO) < 0) _exit(127);
        close(output[1]);
        execv(arguments[0], arguments);
        perror("AquaHub exec");
        _exit(127);
    }
    close(output[1]);
    char buffer[8192];
    for (;;) {
        ssize_t count = read(output[0], buffer, sizeof buffer);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) break;
        write_all(STDOUT_FILENO, buffer, (size_t)count);
        if (log_descriptor >= 0) write_all(log_descriptor, buffer, (size_t)count);
    }
    close(output[0]);
    int status;
    while (waitpid(child, &status, 0) < 0)
        if (errno != EINTR) die("Impossible de lire le résultat de l’installation.");
    if (log_descriptor >= 0) fsync(log_descriptor);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128;
}

static void write_source(void) {
    const char *path = INSTALLER_STATE "/aquahub.sources.list";
    int descriptor = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    if (descriptor < 0) die("Impossible de préparer le dépôt hors ligne.");
    const char *source = "deb [signed-by=" PAYLOAD "/repo-key.gpg] file:" PAYLOAD "/repository ./\n";
    if (write(descriptor, source, strlen(source)) != (ssize_t)strlen(source) || close(descriptor))
        die("Impossible d’écrire le dépôt hors ligne.");
}

static size_t packages(char **result) {
    FILE *stream = fopen(PAYLOAD "/package-list.txt", "re");
    if (!stream) die("La liste des composants hors ligne est absente.");
    char line[512];
    size_t count = 0;
    while (fgets(line, sizeof line, stream)) {
        size_t length = strlen(line);
        if (length == sizeof line - 1 && line[length - 1] != '\n') die("Nom de paquet trop long.");
        while (length && (line[length-1] == '\n' || line[length-1] == '\r')) line[--length] = 0;
        if (!length || line[0] == '#') continue;
        if (line[0] < 'a' || line[0] > 'z' || !strchr(line, '=')) die("La liste doit contenir des paquets épinglés à leur version.");
        for (size_t n = 0; n < length; n++) {
            char c = line[n];
            if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || strchr("+.-:~_=", c)))
                die("Un nom de paquet contient un caractère interdit.");
        }
        if (count >= MAX_PACKAGES) die("La liste contient trop de paquets.");
        result[count] = strdup(line);
        if (!result[count++]) die("Mémoire insuffisante.");
    }
    if (ferror(stream)) die("Impossible de lire la liste des composants.");
    fclose(stream);
    if (!count) die("Le kit ne contient aucun composant à installer.");
    result[count] = NULL;
    return count;
}

int main(int argc, char **argv) {
    (void) argv;
    if (argc != 1) die("Cet installateur n’accepte pas d’arguments.");
    if (geteuid() != 0) die("Une autorisation administrateur est nécessaire.");
    supported_platform();
    const char *source_uid = getenv("PKEXEC_UID");
    if (!source_uid || !*source_uid) source_uid = getenv("SUDO_UID");
    if (!source_uid || !*source_uid || strspn(source_uid, "0123456789") != strlen(source_uid))
        die("Impossible d’identifier le compte opérateur. Ouvrez l’assistant depuis votre session.");
    errno = 0;
    unsigned long operator_uid = strtoul(source_uid, NULL, 10);
    if (errno || !operator_uid || operator_uid > UINT_MAX || !getpwuid((uid_t)operator_uid))
        die("Le compte opérateur n’est pas valide.");
    char uid_text[32];
    snprintf(uid_text, sizeof uid_text, "%lu", operator_uid);
    clearenv();
    setenv("PATH", "/usr/sbin:/usr/bin:/sbin:/bin", 1);
    setenv("HOME", "/root", 1);
    setenv("LANG", "C.UTF-8", 1);
    setenv("DEBIAN_FRONTEND", "noninteractive", 1);
    umask(0077);
    signal(SIGPIPE, SIG_IGN);
    struct statvfs disk;
    if (statvfs("/var/lib", &disk)) die("Impossible de vérifier l’espace disque disponible.");
    if ((unsigned long long)disk.f_bavail * disk.f_frsize < 10ULL * 1024 * 1024 * 1024)
        die("Il faut au moins 10 Gio libres pour les composants, images et sauvegardes. Aucun paquet système n’a été installé.");
    struct stat parent;
    if (mkdir("/var/lib/aquahub", 0755) && errno != EEXIST) die("Impossible de créer le dossier AquaHub.");
    if (lstat("/var/lib/aquahub", &parent) || !S_ISDIR(parent.st_mode) || parent.st_uid || parent.st_mode & 0022)
        die("Le dossier AquaHub n’est pas protégé.");
    private_directory(INSTALLER_STATE);
    int lock = open(INSTALLER_STATE "/setup.lock", O_RDWR | O_CREAT | O_NOFOLLOW, 0600);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) die("Une installation AquaHub est déjà en cours.");
    if (rename(INSTALLER_STATE "/installation.log", INSTALLER_STATE "/installation.previous.log") && errno != ENOENT)
        die("Impossible de conserver le journal d’installation précédent.");
    log_descriptor = open(INSTALLER_STATE "/installation.log", O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (log_descriptor < 0) die("Impossible d’enregistrer le journal d’installation.");
    private_directory("/var/lib/aquahub/agent");
    int maintenance = open("/var/lib/aquahub/agent/maintenance.lock", O_RDWR | O_CREAT | O_NOFOLLOW, 0600);
    if (maintenance < 0 || flock(maintenance, LOCK_EX | LOCK_NB))
        die("Une opération AquaHub est en cours. Attendez sa fin avant de relancer l’installation.");
    trusted_regular(PAYLOAD "/SHA256SUMS");
    trusted_regular(PAYLOAD "/package-list.txt");
    trusted_regular(PAYLOAD "/repo-key.gpg");
    trusted_regular("/usr/lib/aquahub/installer/finish.py");
    if (chdir(PAYLOAD)) die("Le kit hors ligne est introuvable.");
    phase("Vérification du kit hors ligne…");
    char *verify[] = {"/usr/bin/sha256sum", "--check", "--strict", "SHA256SUMS", NULL};
    if (run(verify)) die("Le kit est incomplet ou a été modifié. Aucune installation n’a été lancée.");
    private_directory(INSTALLER_STATE "/lists");
    private_directory(INSTALLER_STATE "/lists/partial");
    private_directory(INSTALLER_STATE "/archives");
    private_directory(INSTALLER_STATE "/archives/partial");
    write_source();
    char *components[MAX_PACKAGES+1];
    size_t count = packages(components);
    char *options[] = {
        "/usr/bin/apt-get",
        "-o", "Dir::Etc::sourcelist=" INSTALLER_STATE "/aquahub.sources.list",
        "-o", "Dir::Etc::sourceparts=-",
        "-o", "Dir::State::lists=" INSTALLER_STATE "/lists",
        "-o", "Dir::Cache::archives=" INSTALLER_STATE "/archives",
        "-o", "Dir::Etc::trustedparts=-",
        "-o", "Acquire::Languages=none",
        "-o", "Acquire::Retries=0",
        "-o", "Acquire::AllowInsecureRepositories=false",
        "-o", "APT::Get::AllowUnauthenticated=false",
        "-o", "DPkg::Lock::Timeout=60",
        "-o", "Dpkg::Options::=--force-confold",
        NULL
    };
    size_t option_count = 0;
    while (options[option_count]) option_count++;
    char *command[MAX_PACKAGES+64];
    for (size_t n=0; n<option_count; n++) command[n] = options[n];
    command[option_count] = "update";
    command[option_count+1] = NULL;
    phase("Préparation du dépôt local signé…");
    if (run(command)) die("Le dépôt hors ligne n’a pas pu être validé. Vérifiez le kit et la date du poste.");
    command[option_count] = "install";
    command[option_count+1] = "--yes";
    command[option_count+2] = "--no-install-recommends";
    for (size_t n=0; n<count; n++) command[option_count+3+n] = components[n];
    command[option_count+3+count] = NULL;
    phase("Installation des composants depuis le kit…");
    if (run(command)) die("Une installation de paquet a échoué. Vous pouvez reprendre l’assistant.");
    phase("Configuration finale d’AquaHub…");
    char *finish[] = {"/usr/bin/python3", "-I", "/usr/lib/aquahub/installer/finish.py", "--operator-uid", uid_text, NULL};
    int status = run(finish);
    for (size_t n=0; n<count; n++) free(components[n]);
    close(lock);
    close(maintenance);
    close(log_descriptor);
    return status;
}
