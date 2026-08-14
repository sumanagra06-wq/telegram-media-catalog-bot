import pytest

from app.metadata import MetadataParseError, parse_metadata


@pytest.mark.parametrize("episode", range(1, 9))
def test_maamla_legal_hai_samples(episode: int) -> None:
    caption = (
        f"Maamla Legal Hai S02E{episode:02d} 1080p Hindi WEB DL 5 1 ESub x264 Mov mkv\n\n"
        "⚠️ ❌👉This file automatically❗delete after 1 minute❗so please forward in another chat👈❌"
    )
    parsed = parse_metadata(caption)
    assert parsed.title == "Maamla Legal Hai"
    assert parsed.season == 2
    assert parsed.episode == episode
    assert parsed.quality == "1080p"
    assert parsed.languages == ["Hindi"]
    assert parsed.year is None


@pytest.mark.parametrize("episode", range(1, 7))
@pytest.mark.parametrize("label", ["", "Name: ", "Title: ", "Series - "])
def test_operation_safed_sagar_separate_episode_caption_pattern(episode: int, label: str) -> None:
    filename = f"Operation Safed Sagar The Highest Air Force Mission S01E{episode:02d} 1 mkv"
    caption = (
        f"{label}{filename}\n\n"
        "⚠️ ❌👉This file automatically❗delete after 1 minute❗so please forward "
        "in another chat👈❌"
    )
    parsed = parse_metadata(caption, filename)
    assert parsed.title == "Operation Safed Sagar The Highest Air Force Mission"
    assert parsed.season == 1
    assert parsed.episode == episode
    assert parsed.year is None
    assert parsed.languages == []
    assert parsed.quality is None


def test_title_that_is_a_year_is_not_erased_by_canonicalization() -> None:
    parsed = parse_metadata("Title: 1984")
    assert parsed.title == "1984"
    assert parsed.year == 1984


def test_year_named_series_still_removes_episode_suffix() -> None:
    parsed = parse_metadata("Title: 1923 S01E01 1080p English mkv")
    assert parsed.title == "1923"
    assert parsed.year == 1923
    assert parsed.season == 1
    assert parsed.episode == 1


def test_lost_hyphenated_season_episode() -> None:
    parsed = parse_metadata("📁 LOST S02-E19 720p x265 Esubs mkv")
    assert parsed.title == "LOST"
    assert parsed.season == 2
    assert parsed.episode == 19
    assert parsed.quality == "720p"
    assert parsed.languages == []


@pytest.mark.parametrize("part", [1, 2, 3])
def test_game_of_thrones_split_season_pack(part: int) -> None:
    name = (
        "Game.Of.Thrones.S01.720p.10Bit.BluRay.Hindi.ORG.2.0-English."
        f"HEVC.x265-HDHub4u.Tv.zip.zip.{part:03d}"
    )
    parsed = parse_metadata(name)
    assert parsed.title == "Game of Thrones"
    assert parsed.season == 1
    assert parsed.episode is None
    assert parsed.pack_part == part
    assert parsed.quality == "720p"
    assert parsed.languages == ["Hindi", "English"]


def test_game_of_thrones_spaced_episode_and_leading_uploader() -> None:
    caption = (
        "@UHDPrime Game of Thrones S03 E01 BluRay 720p Hindi 2 0   English mkv\n\n"
        "⚠️ ❌👉This file automatically❗delete after 1 minute❗so please forward in another chat👈❌"
    )
    parsed = parse_metadata(caption)
    assert parsed.title == "Game of Thrones"
    assert parsed.season == 3
    assert parsed.episode == 1
    assert parsed.quality == "720p"
    assert parsed.languages == ["Hindi", "English"]


def test_labeled_movie_caption_extracts_allowlisted_fields_only() -> None:
    caption = """🎬 Title: Dune Part Two
Year: 2024
Language: Hindi + English
Quality: 4K
HEVC x265 10bit WEB-DL
Join @SomeChannel
Please forward this file
"""
    parsed = parse_metadata(caption, "Dune.Part.Two.2024.2160p.mkv")
    assert parsed.title == "Dune Part Two"
    assert parsed.year == 2024
    assert parsed.languages == ["Hindi", "English"]
    assert parsed.quality == "2160p"
    assert parsed.season is None
    assert parsed.episode is None


def test_filename_fallback_for_movie_pattern() -> None:
    parsed = parse_metadata(None, "Interstellar.2014.1080p.Hindi-English.WEB-DL.x265.mkv")
    assert parsed.title == "Interstellar"
    assert parsed.year == 2014
    assert parsed.quality == "1080p"
    assert parsed.languages == ["Hindi", "English"]


def test_multiline_unlabeled_title_uses_first_line_and_scans_remaining_metadata() -> None:
    parsed = parse_metadata("Dune Part Two\nYear: 2024\nLanguage: Hindi, English\nQuality: 2160p")
    assert parsed.title == "Dune Part Two"
    assert parsed.year == 2024
    assert parsed.languages == ["Hindi", "English"]
    assert parsed.quality == "2160p"


def test_movies_in_filename_caption_is_parsed_without_boilerplate() -> None:
    filename = (
        "Stand by Me Doraemon 2 (2020) 720p HEVC 10bit BDRip [Hindi 2.0-Eng 2.0-Jap 2.0] ESub.mkv"
    )
    parsed = parse_metadata(f"MOVIES.IN:\n\n{filename}", filename)

    assert parsed.title == "Stand by Me Doraemon 2"
    assert parsed.year == 2020
    assert parsed.quality == "720p"
    assert parsed.languages == ["Hindi", "English", "Japanese"]
    assert parsed.season is None


def test_numbered_movie_caption_uses_title_and_following_labels() -> None:
    parsed = parse_metadata(
        "Movie No.37: Doraemon Nobita Little Star War Movie\n"
        "🔵 Quality: 1080P FHD\n"
        "🎙️ language: Hindi Dubbed"
    )

    assert parsed.title == "Doraemon Nobita Little Star War Movie"
    assert parsed.year is None
    assert parsed.quality == "1080p"
    assert parsed.languages == ["Hindi"]


MIXED_DORAEMON_CAPTION = """MOVIES.IN:

Stand by Me Doraemon 2 (2020) 720p HEVC 10bit BDRip [Hindi 2.0-Eng 2.0-Jap 2.0] ESub.mkv

Movie No.37: Doraemon Nobita Little Star War Movie

🔵 Quality: 1080P FHD

🎙️ language: Hindi Dubbed FanPeaky Blinders Webseries Hindi:

Doraemon S01E25 [RareToonsIndia].mkv

Doraemon S01E38 [RareToonsIndia].mkv

Doraemon S02E23 [RareToonsIndia].mkv

Doraemon.S18.Ep.1to15.Combined.Multi.Audio+Hindi.@BoY_51.mkv

Doraemon.S18.Ep31To40.Hindi+Multi.Audio.@BoY_51.mkv

🔴Uploaded By: Nobi Dubber
"""


def test_mixed_caption_uses_attached_episode_filename_as_authority() -> None:
    filename = "Doraemon S01E25 [RareToonsIndia].mkv"
    parsed = parse_metadata(MIXED_DORAEMON_CAPTION, filename)

    assert parsed.title == "Doraemon"
    assert parsed.season == 1
    assert parsed.episode == 25
    assert parsed.episode_start is None
    assert parsed.languages == []
    assert parsed.quality is None
    assert parsed.year is None


@pytest.mark.parametrize(
    ("filename", "start", "end"),
    [
        ("Doraemon.S18.Ep.1to15.Combined.Multi.Audio+Hindi.@BoY_51.mkv", 1, 15),
        ("Doraemon.S18.Ep31To40.Hindi+Multi.Audio.@BoY_51.mkv", 31, 40),
    ],
)
def test_combined_episode_ranges_become_season_packs(
    filename: str,
    start: int,
    end: int,
) -> None:
    parsed = parse_metadata(MIXED_DORAEMON_CAPTION, filename)

    assert parsed.title == "Doraemon"
    assert parsed.season == 18
    assert parsed.episode is None
    assert parsed.episode_start == start
    assert parsed.episode_end == end
    assert parsed.pack_part == start
    assert parsed.languages == ["Hindi"]


def test_empty_input_fails_cleanly() -> None:
    with pytest.raises(MetadataParseError, match="No caption"):
        parse_metadata(None, None)
