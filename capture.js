// Read-only Music automation. Never request account or file-location properties.
function run(argv) {
    var music = Application('com.apple.Music');
    var warnings = [];
    function read(object, key) {
        try { var value = object[key](); return value === undefined ? null : value; }
        catch (error) { warnings.push(key + ': ' + String(error)); return null; }
    }
    if (!music.running()) return JSON.stringify({captured_at:new Date().toISOString(), running:false, player_state:'not_running', track:null, playlist:null, warnings:[]});
    if (argv[0] === 'playlist') {
        // Export returns XML in memory; no Music library changes.
        var playlist = music.currentPlaylist();
        return music.export(playlist, {as:'XML'});
    }
    var state = read(music, 'playerState');
    var track = null, playlist = null;
    try {
        var t = music.currentTrack();
        var name = read(t, 'name');
        if (name !== null) track = {
            name:name, artist:read(t,'artist'), album:read(t,'album'),
            album_artist:read(t,'albumArtist'), duration_seconds:read(t,'duration'),
            disc_number:read(t,'discNumber'), track_number:read(t,'trackNumber'),
            local_persistent_id:read(t,'persistentID'), local_database_id:read(t,'databaseID'),
            local_index:read(t,'index'), cloud_status:read(t,'cloudStatus'),
            stream_url:read(t,'class') === 'URLTrack' ? read(t,'address') : null
        };
    } catch (error) { warnings.push('currentTrack: ' + String(error)); }
    try {
        var p = music.currentPlaylist();
        var pn = read(p,'name');
        if (pn !== null) playlist = {
            name:pn, local_persistent_id:read(p,'persistentID'),
            kind:read(p,'class'), description:read(p,'description'),
            track_count:p.tracks.length
        };
    } catch (error) { warnings.push('currentPlaylist: ' + String(error)); }
    var position = read(music,'playerPosition');
    // Detect a song switch during the multi-property read.
    if (track && read(music.currentTrack(),'persistentID') !== track.local_persistent_id)
        throw Error('Song changed during capture; retry on next sample');
    return JSON.stringify({captured_at:new Date().toISOString(), running:true, player_state:state, position_seconds:position,
        track:track, playlist:playlist, shuffle_enabled:read(music,'shuffleEnabled'),
        shuffle_mode:read(music,'shuffleMode'), repeat_mode:read(music,'songRepeat'), warnings:warnings});
}
