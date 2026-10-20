/* AquaHub first-run assistant. GTK4 is supplied by the Debian GNOME desktop.
 * Python, WebKit and Docker are installed by the separately authorized helper.
 */
#include <gtk/gtk.h>
#include <unistd.h>

typedef struct {
    GtkApplication *application;
    GtkWidget *window, *status, *install, *open, *spinner, *details;
    gboolean running;
    gboolean force_setup;
} Assistant;

static gboolean open_application(Assistant *assistant) {
    GError *error = NULL;
    const gchar *command[] = {"/usr/bin/aquahub", NULL};
    GSubprocess *process = g_subprocess_newv(command, G_SUBPROCESS_FLAGS_NONE, &error);
    if (!process) {
        gtk_label_set_text(GTK_LABEL(assistant->status), error->message);
        g_clear_error(&error);
        return FALSE;
    }
    g_object_unref(process);
    return TRUE;
}

static void open_clicked(GtkButton *button, gpointer data) {
    (void) button;
    Assistant *assistant = data;
    if (open_application(assistant))
        g_application_quit(G_APPLICATION(assistant->application));
}

static gboolean close_requested(GtkWindow *window, gpointer data) {
    (void) window;
    Assistant *assistant = data;
    if (assistant->running)
        gtk_label_set_text(GTK_LABEL(assistant->status), "Installation en cours. Laissez cette fenêtre ouverte jusqu’au résultat.");
    return assistant->running;
}

typedef struct {
    Assistant *assistant;
    GDataInputStream *stream;
} OutputReader;

static void read_output(GObject *source, GAsyncResult *result, gpointer data) {
    OutputReader *reader = data;
    GError *error = NULL;
    gsize length = 0;
    gchar *line = g_data_input_stream_read_line_finish(G_DATA_INPUT_STREAM(source), result, &length, &error);
    if (!line) {
        g_clear_error(&error);
        g_object_unref(reader->stream);
        g_free(reader);
        return;
    }
    gchar *valid = g_utf8_make_valid(line, (gssize)length);
    GtkTextBuffer *buffer = gtk_text_view_get_buffer(GTK_TEXT_VIEW(reader->assistant->details));
    GtkTextIter end, start;
    gtk_text_buffer_get_end_iter(buffer, &end);
    gtk_text_buffer_insert(buffer, &end, valid, -1);
    gtk_text_buffer_insert(buffer, &end, "\n", 1);
    int count = gtk_text_buffer_get_char_count(buffer);
    if (count > 32768) {
        gtk_text_buffer_get_start_iter(buffer, &start);
        gtk_text_buffer_get_iter_at_offset(buffer, &end, count - 30000);
        gtk_text_buffer_delete(buffer, &start, &end);
    }
    if (reader->assistant->running) {
        const gchar *message = NULL;
        if (strstr(valid, "Vérification du kit")) message = "Vérification de l’intégrité du kit hors ligne…";
        else if (strstr(valid, "Préparation du dépôt")) message = "Validation du dépôt local signé…";
        else if (strstr(valid, "Installation des composants")) message = "Installation des dépendances depuis le kit…";
        else if (strstr(valid, "\"phase\": \"docker\"")) message = "Vérification du moteur Docker natif…";
        else if (strstr(valid, "\"phase\": \"bundle\"")) message = "Préparation du bundle Aqua Hub et de ses images…";
        else if (strstr(valid, "\"phase\": \"portainer\"")) message = "Vérification de Portainer…";
        else if (strstr(valid, "\"phase\": \"agent\"")) message = "Installation du suivi AquaHub…";
        else if (strstr(valid, "\"phase\": \"checks\"")) message = "Contrôle du démarrage des composants…";
        if (message) gtk_label_set_text(GTK_LABEL(reader->assistant->status), message);
    }
    g_free(valid);
    g_free(line);
    g_data_input_stream_read_line_async(reader->stream, G_PRIORITY_DEFAULT, NULL, read_output, reader);
}

static void watch_output(Assistant *assistant, GInputStream *stream) {
    OutputReader *reader = g_new0(OutputReader, 1);
    reader->assistant = assistant;
    reader->stream = g_data_input_stream_new(stream);
    g_data_input_stream_read_line_async(reader->stream, G_PRIORITY_DEFAULT, NULL, read_output, reader);
}

static void installation_finished(GObject *source, GAsyncResult *result, gpointer data) {
    Assistant *assistant = data;
    GError *error = NULL;
    gboolean waited = g_subprocess_wait_finish(G_SUBPROCESS(source), result, &error);
    assistant->running = FALSE;
    gtk_spinner_stop(GTK_SPINNER(assistant->spinner));
    gtk_widget_set_sensitive(assistant->install, TRUE);
    gboolean successful = waited && g_subprocess_get_successful(G_SUBPROCESS(source));
    if (successful) {
        gtk_label_set_text(GTK_LABEL(assistant->status), "Installation terminée. Ouvrez AquaHub pour consulter l’état des services.");
        gtk_widget_set_sensitive(assistant->open, TRUE);
        gtk_button_set_label(GTK_BUTTON(assistant->install), "Vérifier / réparer");
    } else {
        int status = waited && g_subprocess_get_if_exited(G_SUBPROCESS(source)) ? g_subprocess_get_exit_status(G_SUBPROCESS(source)) : -1;
        gtk_label_set_text(GTK_LABEL(assistant->status), status == 126 || status == 127
            ? "L’autorisation administrateur a été annulée ou refusée. Vous pouvez réessayer."
            : "L’installation n’a pas abouti. Consultez le détail puis réessayez après correction.");
        gtk_button_set_label(GTK_BUTTON(assistant->install), "Réessayer");
    }
    g_clear_error(&error);
    g_object_unref(source);
}

static void install_clicked(GtkButton *button, gpointer data) {
    (void) button;
    Assistant *assistant = data;
    if (assistant->running) return;
    const gchar *command[] = {"/usr/bin/pkexec", "/usr/lib/aquahub/setup-helper", NULL};
    GError *error = NULL;
    GSubprocess *process = g_subprocess_newv(command,
        G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_PIPE, &error);
    if (!process) {
        gtk_label_set_text(GTK_LABEL(assistant->status), error->message);
        g_clear_error(&error);
        return;
    }
    assistant->running = TRUE;
    gtk_spinner_start(GTK_SPINNER(assistant->spinner));
    gtk_widget_set_sensitive(assistant->install, FALSE);
    gtk_widget_set_sensitive(assistant->open, FALSE);
    gtk_label_set_text(GTK_LABEL(assistant->status), "Autorisez l’installation, puis patientez. Tous les composants viennent du kit hors ligne.");
    gtk_text_buffer_set_text(gtk_text_view_get_buffer(GTK_TEXT_VIEW(assistant->details)), "", 0);
    watch_output(assistant, g_subprocess_get_stdout_pipe(process));
    watch_output(assistant, g_subprocess_get_stderr_pipe(process));
    g_subprocess_wait_async(process, NULL, installation_finished, assistant);
}

static void activate(GtkApplication *application, gpointer data) {
    Assistant *assistant = data;
    assistant->application = application;
    if (assistant->window) {
        gtk_window_present(GTK_WINDOW(assistant->window));
        return;
    }
    GtkCssProvider *style = gtk_css_provider_new();
    gtk_css_provider_load_from_string(style,
        "button.suggested-action { background-image: none; background-color: @theme_fg_color; color: @theme_bg_color; border-color: @theme_fg_color; }"
        "button.suggested-action:hover { opacity: .88; }");
    gtk_style_context_add_provider_for_display(gdk_display_get_default(), GTK_STYLE_PROVIDER(style), GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    g_object_unref(style);
    assistant->window = gtk_application_window_new(application);
    gtk_window_set_title(GTK_WINDOW(assistant->window), "AquaHub — Installation");
    gtk_window_set_default_size(GTK_WINDOW(assistant->window), 650, 570);
    g_signal_connect(assistant->window, "close-request", G_CALLBACK(close_requested), assistant);

    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 18);
    gtk_widget_set_margin_top(box, 32);
    gtk_widget_set_margin_bottom(box, 28);
    gtk_widget_set_margin_start(box, 36);
    gtk_widget_set_margin_end(box, 36);
    gtk_window_set_child(GTK_WINDOW(assistant->window), box);
    GtkWidget *title = gtk_label_new("AquaHub");
    gtk_widget_add_css_class(title, "title-1");
    gtk_label_set_xalign(GTK_LABEL(title), 0);
    gtk_box_append(GTK_BOX(box), title);
    GtkWidget *subtitle = gtk_label_new("Votre poste Aqua Hub, prêt à l’emploi.");
    gtk_label_set_xalign(GTK_LABEL(subtitle), 0);
    gtk_box_append(GTK_BOX(box), subtitle);
    GtkWidget *description = gtk_label_new("Cet assistant installe les composants inclus dans votre kit : AquaHub, Docker, Portainer et le bundle Aqua Hub.\n\nL’installation fonctionne sans Internet et nécessite votre autorisation administrateur. La configuration réseau existante est conservée.");
    gtk_label_set_wrap(GTK_LABEL(description), TRUE);
    gtk_label_set_xalign(GTK_LABEL(description), 0);
    gtk_box_append(GTK_BOX(box), description);
    assistant->status = gtk_label_new("Prêt à installer. Aucun changement n’a encore été effectué.");
    gtk_label_set_wrap(GTK_LABEL(assistant->status), TRUE);
    gtk_label_set_xalign(GTK_LABEL(assistant->status), 0);
    gtk_box_append(GTK_BOX(box), assistant->status);

    GtkWidget *actions = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 12);
    assistant->spinner = gtk_spinner_new();
    assistant->install = gtk_button_new_with_label("Installer AquaHub");
    gtk_widget_add_css_class(assistant->install, "suggested-action");
    g_signal_connect(assistant->install, "clicked", G_CALLBACK(install_clicked), assistant);
    assistant->open = gtk_button_new_with_label("Ouvrir AquaHub");
    gtk_widget_set_sensitive(assistant->open, access("/var/lib/aquahub/setup.complete", F_OK) == 0);
    g_signal_connect(assistant->open, "clicked", G_CALLBACK(open_clicked), assistant);
    gtk_box_append(GTK_BOX(actions), assistant->install);
    gtk_box_append(GTK_BOX(actions), assistant->open);
    gtk_box_append(GTK_BOX(actions), assistant->spinner);
    gtk_box_append(GTK_BOX(box), actions);

    GtkWidget *expander = gtk_expander_new("Détail de l’installation");
    GtkWidget *scroll = gtk_scrolled_window_new();
    gtk_widget_set_size_request(scroll, -1, 150);
    assistant->details = gtk_text_view_new();
    gtk_text_view_set_editable(GTK_TEXT_VIEW(assistant->details), FALSE);
    gtk_text_view_set_monospace(GTK_TEXT_VIEW(assistant->details), TRUE);
    gtk_text_view_set_wrap_mode(GTK_TEXT_VIEW(assistant->details), GTK_WRAP_WORD_CHAR);
    gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), assistant->details);
    gtk_expander_set_child(GTK_EXPANDER(expander), scroll);
    gtk_box_append(GTK_BOX(box), expander);
    gtk_window_present(GTK_WINDOW(assistant->window));
}

int main(int argc, char **argv) {
    Assistant assistant = {0};
    /* --setup is consumed by /usr/bin/aquahub; no root GUI is ever started. */
    GtkApplication *application = gtk_application_new("com.aquadrone.aquahub.setup", G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(application, "activate", G_CALLBACK(activate), &assistant);
    int result = g_application_run(G_APPLICATION(application), argc, argv);
    g_object_unref(application);
    return result;
}
